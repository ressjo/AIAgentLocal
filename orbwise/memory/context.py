"""Gesprächsverlauf mit festem Token-Budget.

Wird der Verlauf zu lang, werden die ältesten Nachrichten per LLM in eine "laufende
Zusammenfassung" gefaltet. Nichts geht verloren: Alles steht zusätzlich im Tages-Journal
und im Suchindex und wird bei Bedarf per Retrieval zurückgeholt.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger(__name__)

SUMMARY_MAX_CHARS = 2400
TOOL_SNIPPET_CHARS = 400


def est_tokens(text: str) -> int:
    # Grobe, bewusst konservative Schätzung (Deutsch ≈ 3–3,5 Zeichen pro Token)
    return len(text) // 3 + 1


def msg_tokens(msg: dict) -> int:
    n = est_tokens(msg.get("content") or "") + 4
    if msg.get("tool_calls"):
        n += est_tokens(json.dumps(msg["tool_calls"], ensure_ascii=False))
    return n


def render_for_summary(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m["role"]
        content = (m.get("content") or "").strip()
        if role == "user":
            lines.append(f"Nutzer: {content}")
        elif role == "assistant":
            if content:
                lines.append(f"Jarvis: {content}")
            for call in m.get("tool_calls") or []:
                fn = call.get("function", {})
                lines.append(f"Jarvis ruft Tool {fn.get('name')} auf: {json.dumps(fn.get('arguments'), ensure_ascii=False)}")
        elif role == "tool":
            snippet = content[:TOOL_SNIPPET_CHARS] + ("…" if len(content) > TOOL_SNIPPET_CHARS else "")
            lines.append(f"Tool-Ergebnis: {snippet}")
    return "\n".join(lines)


def make_title(text: str, limit: int = 60) -> str:
    """Chat-Titel aus der ersten Nutzernachricht."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]) + " …"


class Conversation:
    def __init__(self, state_path: Path | None = None, chat_id: str = ""):
        self.state_path = state_path
        self.history: list[dict] = []
        self.running_summary = ""
        # Vollständige Fassung gefalteter Werkzeug-Ergebnisse (Nummer → {tool, call, text}) – holt earlier_output
        self.stash: dict[str, dict] = {}
        # Metadaten des Chats (Titel, Stern, Zeitstempel) – siehe memory/chats.py
        self.meta: dict = {"id": chat_id, "title": "", "starred": False, "created": time.time()}
        self.load()

    @property
    def chat_id(self) -> str:
        return self.meta.get("id", "")

    # ---------- Persistenz (Gespräch überlebt Neustarts) ----------
    def load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.history = data.get("history", [])
                self.running_summary = data.get("running_summary", "")
                self.stash = data.get("stash") or {}
                self.meta.update(data.get("meta") or {})
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Sitzungszustand nicht lesbar (%s) – starte neu", e)

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        self.meta["updated"] = time.time()
        data = {"meta": self.meta, "history": self.history, "running_summary": self.running_summary}
        if self.stash:
            data["stash"] = self.stash
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def add(self, msg: dict) -> None:
        msg = dict(msg)
        msg.setdefault("ts", time.time())
        if msg.get("role") == "user" and not self.meta.get("title") and msg.get("content"):
            self.meta["title"] = make_title(msg["content"])
        self.history.append(msg)

    def reset(self) -> None:
        self.history = []
        self.running_summary = ""
        self.stash = {}
        self.save()

    # ---------- Budget ----------
    def history_tokens(self) -> int:
        return sum(msg_tokens(m) for m in self.history)

    def window_start(self) -> float | None:
        """Zeitstempel der ältesten wörtlich vorhandenen Nachricht."""
        return self.history[0].get("ts") if self.history else None

    def _cut_index(self, tokens_to_free: int) -> int:
        """Index, bis zu dem gefaltet wird. Schnitt nur vor einer User-Nachricht, damit
        Tool-Calls und ihre Ergebnisse zusammen bleiben; die letzte User-Nachricht bleibt immer."""
        user_idx = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(user_idx) < 2:
            return 0
        freed, cut = 0, 0
        for i, m in enumerate(self.history):
            if i in user_idx and i > 0:
                cut = i
                if freed >= tokens_to_free or i == user_idx[-1]:
                    break
            freed += msg_tokens(m)
        return min(cut, user_idx[-1])

    def age_tool_results(self, keep_turns: int = 1) -> int:
        """Lange Tool-Ergebnisse älterer Runden auf einen Auszug kürzen – die letzten keep_turns Runden bleiben
        vollständig (für Nachfragen). Einmalig pro Ergebnis, damit der Anfang des Prompts danach stabil bleibt.
        Liefert die Zahl gekürzter Ergebnisse."""
        users = [i for i, m in enumerate(self.history) if m["role"] == "user"]
        if len(users) <= keep_turns:
            return 0
        limit = users[-keep_turns]
        n = 0
        for m in self.history[:limit]:
            content = m.get("content") or ""
            if m["role"] != "tool" or len(content) <= TOOL_AGE_CHARS * 2 or content.endswith(AGED_NOTE):
                continue
            m["content"] = content[:TOOL_AGE_CHARS].rstrip() + AGED_NOTE
            n += 1
        return n

    # ---------- Lange Aufgaben: ältere Schritte der laufenden Runde falten ----------
    def turn_start(self) -> int:
        return max((i for i, m in enumerate(self.history) if m["role"] == "user"), default=0)

    def turn_tokens(self) -> int:
        return sum(msg_tokens(m) for m in self.history[self.turn_start():])

    def fold_current_turn(self, budget: int, keep_last: int = 2, trigger: float = 0.75,
                          target: float = 0.5) -> tuple[int, int]:
        """Braucht die laufende Runde mehr als trigger·budget, werden ihre älteren Werkzeug-Ergebnisse (ohne die
        letzten keep_last Schritte) auf einen Auszug gefaltet, bis target·budget erreicht ist – auf einmal, damit
        der Prompt-Anfang danach stabil bleibt (KV-Cache). Die volle Fassung bleibt in stash und ist per
        earlier_output abrufbar. Frage, eigene Texte des Modells und kurze Ergebnisse bleiben unverändert.
        Liefert (gefaltete Schritte, gesparte Token)."""
        start = self.turn_start()
        used = self.turn_tokens()
        if used <= int(budget * trigger):
            return 0, 0
        goal = used - int(budget * target)
        calls = [i for i in range(start, len(self.history))
                 if self.history[i]["role"] == "assistant" and self.history[i].get("tool_calls")]
        protected_from = calls[-keep_last] if keep_last and len(calls) >= keep_last else len(self.history)
        folded = freed = 0
        call_text = {i: call_for(self.history, i) for i in range(start, protected_from)
                     if self.history[i]["role"] == "tool"}  # vor dem Kürzen der Argumente festhalten
        for i in range(start, protected_from):
            if freed >= goal:
                break
            m = self.history[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                before = msg_tokens(m)
                m["tool_calls"] = [shorten_call(c) for c in m["tool_calls"]]
                freed += before - msg_tokens(m)
                continue
            content = m.get("content") or ""
            if m["role"] != "tool" or m.get("folded") or len(content) <= FOLD_KEEP_CHARS:
                continue
            again = re.match(r"\[Schritt (\d+) · ", content) if m.get("tool_name") == "earlier_output" else None
            n = again.group(1) if again and again.group(1) in self.stash else \
                str(max(map(int, self.stash), default=0) + 1)  # schon geholte Ausgabe nicht doppelt ablegen
            short = digest(content, n)
            if len(short) + 100 > len(content):
                continue  # lohnt nicht
            if not (again and n == again.group(1)):
                self.stash[n] = {"tool": m.get("tool_name", ""), "call": call_text.get(i, ""), "text": content}
            before = msg_tokens(m)
            m["content"], m["folded"] = short, n
            freed += before - msg_tokens(m)
            folded += 1
        # Reicht das nicht (sehr kleines Fenster, viele Schritte): ältere Auszüge auf ihre Kopfzeile kürzen –
        # der Verweis auf earlier_output bleibt, die volle Fassung liegt weiter im stash
        for i in range(start, protected_from):
            if freed >= goal:
                break
            m = self.history[i]
            if m["role"] == "tool" and m.get("folded") and "\n" in (m.get("content") or ""):
                before = msg_tokens(m)
                m["content"] = m["content"].split("\n", 1)[0]
                freed += before - msg_tokens(m)
        self._trim_stash()
        return folded, max(freed, 0)

    def _trim_stash(self, max_chars: int = 400_000) -> None:
        """Speicher begrenzen: älteste Einträge zuerst (ihr Auszug im Verlauf bleibt)."""
        total = sum(len(v.get("text", "")) for v in self.stash.values())
        for key in sorted(self.stash, key=int):
            if total <= max_chars:
                break
            total -= len(self.stash[key].get("text", ""))
            del self.stash[key]

    def needs_compact(self, budget: int) -> bool:
        return self.history_tokens() > int(budget * 0.85)

    async def compact(self, llm, budget: int) -> bool:
        """Faltet alte Nachrichten, sobald ~85 % des Budgets erreicht sind, bis der Verlauf unter ~60 % liegt."""
        total = self.history_tokens()
        if not self.needs_compact(budget):
            return False
        cut = self._cut_index(total - int(budget * 0.6))
        if cut <= 0:
            return False
        folded, self.history = self.history[:cut], self.history[cut:]
        transcript = render_for_summary(folded)
        prompt = [
            {"role": "system", "content": (
                "Du pflegst die laufende Zusammenfassung eines Gesprächs zwischen einem Nutzer und seinem "
                "KI-Assistenten Jarvis. Schreibe eine kompakte, sachliche Zusammenfassung auf Deutsch "
                "(max. 12 Stichpunkte): Anliegen, Entscheidungen, Ergebnisse, offene Aufgaben, wichtige "
                "Details wie Dateipfade, Paketnamen oder Befehle. Keine Einleitung.")},
            {"role": "user", "content": (
                f"Bisherige Zusammenfassung:\n{self.running_summary or '(leer)'}\n\n"
                f"Neue Gesprächsteile:\n{transcript}\n\nAktualisierte Zusammenfassung:")},
        ]
        try:
            summary = (await llm.chat(prompt)).strip()
        except Exception as e:  # noqa: BLE001 – Fallback: grob abschneiden statt abstürzen
            log.warning("Zusammenfassen fehlgeschlagen (%s) – nutze Kurzfassung", e)
            summary = (self.running_summary + "\n" + transcript[-1200:]).strip()
        self.running_summary = summary[-SUMMARY_MAX_CHARS:]
        self.save()
        return True

    def trimmed_history(self, budget: int) -> list[dict]:
        """Notbremse: falls die Kompaktierung nicht reicht, älteste Teile weglassen.

        Die aktuelle Runde (letzte Nutzerfrage + alle Tool-Aufrufe danach) bleibt immer erhalten – ohne
        Nutzerfrage lehnen manche Chat-Vorlagen (Qwen/Bonsai) die Anfrage ab. Ist sie allein zu groß,
        werden ihre Tool-Ergebnisse gekürzt (die ältesten zuerst)."""
        hist = self.history
        last_user = max((i for i, m in enumerate(hist) if m["role"] == "user"), default=0)
        turn = [dict(m) for m in hist[last_user:]]
        used = sum(msg_tokens(m) for m in turn)
        if used > budget:
            shrink_tool_results(turn, used - budget)
            used = sum(msg_tokens(m) for m in turn)
        earlier: list[dict] = []
        for m in reversed(hist[:last_user]):
            t = msg_tokens(m)
            if used + t > budget:
                break
            earlier.append(m)
            used += t
        earlier.reverse()
        # Nie mit einem verwaisten Tool-Ergebnis beginnen
        while earlier and earlier[0]["role"] == "tool":
            earlier.pop(0)
        return [{k: v for k, v in m.items() if k not in ("ts", "folded")} for m in earlier + turn]


FOLD_KEEP_CHARS = 400  # kürzere Werkzeug-Ergebnisse werden nie gefaltet
FOLD_ARG_CHARS = 300  # längere Argumente (z. B. Dateiinhalt bei write_file) werden im gefalteten Aufruf gekürzt
_KEY_LINE = re.compile(r"error|fehler|warn|fail|denied|verweigert|not found|nicht gefunden|exit|abgelehnt|"
                       r"blockiert|timeout|zeitüberschreitung|permission|cannot|kann nicht|missing|fehlt", re.I)


def digest(content: str, n: str) -> str:
    """Auszug eines gefalteten Werkzeug-Ergebnisses: Status, auffällige Zeilen, letzte Zeile, Größe."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    picked: list[str] = []
    for ln in [lines[0]] + [ln for ln in lines[1:-1] if _KEY_LINE.search(ln)] + [lines[-1]] if lines else []:
        ln = ln if len(ln) <= 160 else ln[:157] + "…"
        if ln not in picked:
            picked.append(ln)
    body, used = [], 0
    for ln in picked:
        if used + len(ln) > 450 and body:
            break
        body.append(ln)
        used += len(ln)
    if body and lines and body[-1] != picked[-1]:  # letzte Zeile (oft das Ergebnis) immer behalten
        body.append(picked[-1])
    return (f"[gefaltet: {len(content)} Zeichen, {len(lines)} Zeilen – vollständig mit earlier_output(step={n})]\n"
            + "\n".join(body))


def shorten_call(call: dict) -> dict:
    fn = call.get("function") or {}
    args = fn.get("arguments")
    if not isinstance(args, dict):
        return call
    short = {k: (f"[{len(v)} Zeichen]" if isinstance(v, str) and len(v) > FOLD_ARG_CHARS else v)
             for k, v in args.items()}
    return call if short == args else {**call, "function": {**fn, "arguments": short}}


def call_for(history: list[dict], tool_index: int) -> str:
    """Der Aufruf (Name + Argumente), zu dem das Werkzeug-Ergebnis an tool_index gehört."""
    k = 0
    for j in range(tool_index - 1, -1, -1):
        m = history[j]
        if m["role"] == "tool":
            k += 1
            continue
        if m["role"] == "assistant" and m.get("tool_calls"):
            calls = m["tool_calls"]
            if k < len(calls):
                fn = calls[k].get("function") or {}
                return f"{fn.get('name', '')} {json.dumps(fn.get('arguments'), ensure_ascii=False)}"
        break
    return ""


TOOL_MIN_CHARS = 600
TOOL_AGE_CHARS = 800
AGED_NOTE = "\n… [gekürzt – Details bei Bedarf mit dem Tool erneut abrufen]"
TRIM_NOTE = "\n… [gekürzt, damit alles ins Kontextfenster passt]"


def shrink_tool_results(messages: list[dict], excess_tokens: int) -> int:
    """Kürzt Tool-Ergebnisse (älteste zuerst) um insgesamt etwa excess_tokens. Liefert die gekürzten Tokens."""
    freed = 0
    for m in messages:
        if freed >= excess_tokens:
            break
        if m["role"] != "tool":
            continue
        content = m.get("content") or ""
        if len(content) <= TOOL_MIN_CHARS:
            continue
        keep = max(TOOL_MIN_CHARS, len(content) - (excess_tokens - freed) * 3 - len(TRIM_NOTE))
        if keep >= len(content):
            continue
        m["content"] = content[:keep] + TRIM_NOTE
        freed += est_tokens(content) - est_tokens(m["content"])
    return freed
