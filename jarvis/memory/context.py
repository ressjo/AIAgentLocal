"""Gesprächsverlauf mit festem Token-Budget.

Wird der Verlauf zu lang, werden die ältesten Nachrichten per LLM in eine "laufende
Zusammenfassung" gefaltet. Nichts geht verloren: Alles steht zusätzlich im Tages-Journal
und im Suchindex und wird bei Bedarf per Retrieval zurückgeholt.
"""

from __future__ import annotations

import json
import logging
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


class Conversation:
    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path
        self.history: list[dict] = []
        self.running_summary = ""
        self.load()

    # ---------- Persistenz (Gespräch überlebt Neustarts) ----------
    def load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.history = data.get("history", [])
                self.running_summary = data.get("running_summary", "")
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Sitzungszustand nicht lesbar (%s) – starte neu", e)

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"history": self.history, "running_summary": self.running_summary},
                                  ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def add(self, msg: dict) -> None:
        msg = dict(msg)
        msg.setdefault("ts", time.time())
        self.history.append(msg)

    def reset(self) -> None:
        self.history = []
        self.running_summary = ""
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

    async def compact(self, llm, budget: int) -> bool:
        """Faltet alte Nachrichten, bis der Verlauf unter ~60 % des Budgets liegt."""
        total = self.history_tokens()
        if total <= budget:
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
        """Notbremse: falls die Kompaktierung nicht reicht, älteste Teile weglassen."""
        out, used = [], 0
        for m in reversed(self.history):
            t = msg_tokens(m)
            if used + t > budget and out:
                break
            out.append(m)
            used += t
        out.reverse()
        # Nie mit einem verwaisten Tool-Ergebnis beginnen
        while out and out[0]["role"] == "tool":
            out.pop(0)
        return [{k: v for k, v in m.items() if k != "ts"} for m in out]
