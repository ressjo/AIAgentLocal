"""Der Agent: baut den Kontext (Gedächtnis + Verlauf), führt die Tool-Schleife aus und
meldet alles als Events an die Oberfläche."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import platform
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from . import prompts, toolselect
from .config import Config
from .llm import ContextOverflow, LLMError, strip_think
from .memory import Memory, est_tokens
from .memory.context import TRIM_NOTE, msg_tokens
from .tools.proc import clip
from .tools.registry import (
    BLOCKED,
    CONFIRM,
    SAFE,
    ToolContext,
    coerce_args,
    get_tool,
    load_all_tools,
    missing_args,
    tool_schemas,
)
from .tools.system import _os_name

log = logging.getLogger(__name__)

# Nach dem Lesen fremder Mailinhalte brauchen auch sonst sichere Tools dieser Gruppen eine Bestätigung –
# eine Mail könnte versteckte Anweisungen enthalten (z. B. Daten per fetch_url nach außen schicken).
TAINT_SOURCES = {"mail_list", "mail_search", "mail_read", "mail_ask", "daily_briefing",
                 "look_at_screen", "look_at_image"}  # auch Bildschirm/Bild: eine Webseite kann Anweisungen zeigen
TAINT_GUARDED = {"shell", "web", "files", "apps", "obsidian", "trilium", "calendar_tools", "homeassistant",
                 "memory_tools", "reminder_tools", "power", "telegram_tools"}


def is_taint_source(name: str, result: str) -> bool:
    """Bringt dieses Tool-Ergebnis fremden Text (Mails) in den Verlauf?"""
    return name in TAINT_SOURCES and (name != "daily_briefing" or "E-Mail:" in result)


def plan_steps(text: str) -> int:
    """Anzahl nummerierter Schritte in einem Plan (für „Mein Plan hat n Schritte“)."""
    return len(re.findall(r"^\s*\d+[.)]\s", text, re.M))


def prompt_size(stats: dict, estimated: int) -> int:
    """Echte Prompt-Größe laut Server – 0, wenn sie unbrauchbar ist: Ollama zählt bei einem Cache-Treffer nur die
    neu verarbeiteten Token (prompt_eval_count); so ein Wert weit unter der Schätzung würde das Budget aufblähen."""
    total = int(stats.get("prompt_total") or 0)
    if stats.get("prompt_cached") is not None:
        return total  # llama-server nennt den Cache-Anteil getrennt – dort ist total die volle Größe
    return 0 if total and total < 0.6 * estimated else total


ANSWER_RESERVE = 1500  # Token, die im Kontextfenster für die Antwort frei bleiben
Emit = Callable[[dict], Awaitable[None]]
Confirm = Callable[[str, str, dict, str], Awaitable[bool]]


class ThinkFilter:
    """Entfernt <think>…</think>-Blöcke aus einem Token-Stream (falls das Modell trotzdem 'denkt')."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False
        self.thought: list[str] = []  # Text innerhalb von <think>, abholbar mit take_thought()

    def take_thought(self) -> str:
        text, self.thought = "".join(self.thought), []
        return text

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                (self.thought if self.inside else out).append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # Möglichen Tag-Anfang am Ende zurückhalten
            keep = 0
            for k in range(1, len(tag)):
                if self.buf.endswith(tag[:k]):
                    keep = k
            (self.thought if self.inside else out).append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:] if keep else ""
            return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest


class Agent:
    def __init__(self, cfg: Config, llm, memory: Memory):
        self.cfg = cfg
        self.llm = llm
        self.memory = memory
        self._budget_scale = 1.0  # < 1, wenn der Server „Kontext zu klein“ gemeldet hat
        # Verhältnis echte/geschätzte Prompt-Token je Modellprofil (lernt aus den Zahlen des Servers)
        self._token_ratio: dict[str, float] = {}
        self._turn_time = ""  # Uhrzeit der aktuellen Anfrage (bleibt über alle Schritte gleich → Cache)
        self._think: bool | None = None
        self._plan = False  # Planmodus: nur lesen, am Ende einen Plan vorlegen
        self.auto_read = True  # Auto-Knopf: erkannte lesende Shell-Befehle ohne Rückfrage ausführen
        self.last_context: dict | None = None  # letzter Prompt-Aufbau (für die Kontext-Anzeige)
        self.tools = load_all_tools()
        self.all_schemas = tool_schemas(cfg)
        self.groups_of = {name: spec.group for name, spec in self.tools.items()}
        self.schemas = self.all_schemas
        self.schema_tokens = est_tokens(json.dumps(self.schemas, ensure_ascii=False))
        self.all_schema_tokens = self.schema_tokens
        self.lock = asyncio.Lock()
        self.services: dict = {}

    # ---------- Prompt ----------
    def system_prompt(self) -> str:
        now = datetime.now()
        c = self.cfg
        date, time_ = prompts.format_date(c, now)
        base = prompts.base_prompt(
            c, name=c.assistant_name, date=date, time=time_, os=_os_name(), host=platform.node(),
            user=c.user_name or getpass.getuser(), home=Path.home(),
            nas=", ".join(map(str, c.tools.nas_paths)) or prompts.text(c, "no_nas"))
        return (base + prompts.hints(c) + c.persona_extra).strip()

    def build_messages(self, hits) -> list[dict]:
        conv = self.memory.conversation
        base = self.system_prompt()
        sections = [base]
        facts = self.memory.facts_text()
        if facts:
            sections.append(prompts.section(self.cfg, "facts") + "\n" + facts)
        if conv.running_summary:
            sections.append(prompts.section(self.cfg, "summary") + "\n" + conv.running_summary)
        system = "\n\n".join(sections)
        # Uhrzeit und Erinnerungen wechseln – sie kommen vor die aktuelle Nutzernachricht, nicht in den
        # System-Prompt, damit der Anfang gleich bleibt (KV-Cache des Modell-Servers)
        note = prompts.context_note(self.cfg, self._turn_time or datetime.now().strftime("%H:%M"),
                                    self.memory.format_hits(hits), plan=self._plan)
        note_t = est_tokens(note)
        total_budget = self.context_budget()
        budget = total_budget - est_tokens(system) - self.schema_tokens - note_t
        history = conv.trimmed_history(max(budget, 1000))
        last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=None)
        if last_user is not None:
            history[last_user] = {**history[last_user], "content": note + (history[last_user].get("content") or "")}
        # Für die Anzeige „Kontext“ in der Oberfläche (Schätzung; echte Server-Token kommen nach dem Schritt)
        base_t, system_t = est_tokens(base), est_tokens(system)
        history_t = sum(msg_tokens(m) for m in history)
        trimmed = len(history) < len(conv.history) or any(
            m["role"] == "tool" and (m.get("content") or "").endswith(TRIM_NOTE) for m in history)
        self.last_context = {
            "window": self.model_window(), "budget": total_budget,
            "used": system_t + self.schema_tokens + history_t,
            "parts": {"system": base_t, "tools": self.schema_tokens, "memory": system_t - base_t + note_t,
                      "history": history_t - note_t},
            "trimmed": trimmed, "summarized": bool(conv.running_summary),
        }
        return [{"role": "system", "content": system}, *history]

    def model_window(self) -> int:
        """Kontextfenster des aktiven Modells (vom Server gemeldet, sonst aus der Config)."""
        num_ctx = getattr(self.llm, "context_size", None)
        if not num_ctx:
            profile = getattr(self.llm, "profile", None)
            num_ctx = getattr(profile, "num_ctx", None) or self.cfg.llm.num_ctx
        return int(num_ctx)

    def context_budget(self) -> int:
        """Prompt-Budget: Kontextfenster des aktiven Modells (optional begrenzt durch context_budget_tokens)
        (abzüglich Platz für die Antwort) – z. B. Bonsai mit 8192 Token."""
        budget = self.model_window() - ANSWER_RESERVE
        if self.cfg.memory.context_budget_tokens:
            budget = min(budget, self.cfg.memory.context_budget_tokens)
        return max(2000, int(budget * self._budget_scale / self.token_ratio()))

    def _profile_key(self) -> str:
        return str(getattr(self.llm, "active", "") or "default")

    def token_ratio(self) -> float:
        return self._token_ratio.get(self._profile_key(), 1.0)

    def learn_tokens(self, estimated: int, real: int) -> None:
        """Schätzung an die echten Token des Servers angleichen (gleitend, begrenzt auf 0,6–1,3)."""
        if estimated < 500 or not real:
            return
        key = self._profile_key()
        sample = max(0.6, min(1.3, real / estimated))
        old = self._token_ratio.get(key)
        self._token_ratio[key] = round(sample if old is None else 0.7 * old + 0.3 * sample, 3)

    def choose_tools(self, used_groups: set[str] | None = None) -> None:
        """Bei kleinem Kontextfenster nur passende Tool-Gruppen mitschicken (siehe toolselect.py)."""
        if self.all_schema_tokens <= 0.3 * self.context_budget():
            self.schemas, self.schema_tokens = self.all_schemas, self.all_schema_tokens
            return
        users = [m.get("content", "") for m in self.memory.conversation.history if m.get("role") == "user"][-2:]
        self.schemas = toolselect.select(self.all_schemas, self.groups_of, users, used_groups or set())
        self.schema_tokens = est_tokens(json.dumps(self.schemas, ensure_ascii=False))

    def history_budget(self) -> int:
        m = self.cfg.memory
        fixed = 900 + m.facts_max_tokens + m.retrieval_max_tokens + 800 + self.schema_tokens
        return max(1500, self.context_budget() - fixed)

    # ---------- Ablauf ----------
    async def run(self, user_text: str, emit: Emit, confirm: Confirm, think: bool | None = None,
                  plan: bool = False) -> str:
        """think: Denkmodus für diese Anfrage (None = Einstellung des Modell-Profils).
        plan: Planmodus – nur lesend nachsehen und einen Plan zur Freigabe vorlegen (gedacht wird nur, wenn der
        Denkmodus an ist)."""
        async with self.lock:
            self._think, self._plan = think, plan
            self._tainted = False
            try:
                return await self._run(user_text, emit, confirm)
            finally:
                self._think, self._plan = None, False

    async def run_in_chat(self, chat_id: str, title: str, text: str, emit: Emit, confirm: Confirm,
                          plan: bool = False, think: bool | None = None) -> tuple[str, str]:
        """Für Routinen und Telegram: Aufgabe in einem eigenen Chat erledigen – der aktive Chat des Nutzers bleibt
        unberührt. Liefert (Antwort, Chat-ID)."""
        async with self.lock:
            self._think, self._plan = think, plan
            self._tainted = False
            try:
                with self.memory.in_chat(chat_id, title) as conv:
                    answer = await self._run(text, emit, confirm)
                    return answer, conv.chat_id
            finally:
                self._think, self._plan = None, False

    async def _run(self, user_text: str, emit: Emit, confirm: Confirm) -> str:
        self._turn_time = datetime.now().strftime("%H:%M")
        conv = self.memory.conversation
        # Steht noch Mail-Text im Verlauf, kann er auch in späteren Anfragen wirken – dann bleibt der Schutz an
        self._tainted = any(m.get("role") == "tool" and is_taint_source(m.get("tool_name", ""), m.get("content") or "")
                            for m in conv.history)
        start_len = len(conv.history)
        await emit({"type": "state", "state": "thinking"})
        hits = await self.memory.retrieve(user_text, exclude_after=conv.window_start())
        conv.add({"role": "user", "content": user_text})
        spoken: list[str] = []
        tool_notes: list[str] = []
        msg_id = uuid.uuid4().hex[:8]
        await emit({"type": "assistant_start", "id": msg_id, "plan": self._plan})
        try:
            seen: dict[str, int] = {}  # gleiche Tool-Aufrufe zählen (Schleifenerkennung)
            finished = False
            used_groups: set[str] = set()
            for _ in range(max(1, self.cfg.tools.max_steps)):
                self.choose_tools(used_groups)
                content, calls = await self._step(hits, emit, msg_id)
                entry = {"role": "assistant", "content": content}
                if calls:
                    entry["tool_calls"] = calls
                conv.add(entry)
                if content:
                    spoken.append(content)
                if not calls:
                    finished = True
                    break
                await emit({"type": "segment_end", "id": msg_id})
                looping = False
                for call in calls:
                    fn = call.get("function", {})
                    used_groups.add(self.groups_of.get(fn.get("name", ""), ""))
                    key = fn.get("name", "") + json.dumps(fn.get("arguments"), sort_keys=True, ensure_ascii=False)
                    seen[key] = seen.get(key, 0) + 1
                    if seen[key] >= 3:
                        # Nicht noch einmal ausführen – das Modell dreht sich im Kreis
                        name = fn.get("name", "")
                        result = prompts.text(self.cfg, "repeat_skipped")
                        note = f"{name}: Wiederholung übersprungen"
                        looping = looping or seen[key] >= 4
                    else:
                        name, result, note = await self._execute(call, emit, confirm)
                    conv.add({"role": "tool", "content": result, "tool_name": name})
                    tool_notes.append(note)
                if looping:
                    break
            if not finished:
                # Limit erreicht oder Schleife: ohne Tools zusammenfassen lassen, statt hart abzubrechen
                content, _ = await self._step(hits, emit, msg_id, final=True)
                if not content:
                    content = prompts.text(self.cfg, "paused")
                    await emit({"type": "token", "id": msg_id, "text": content})
                hint = prompts.text(self.cfg, "continue_hint")
                await emit({"type": "token", "id": msg_id, "text": "\n\n" + hint})
                content = f"{content}\n\n{hint}"
                conv.add({"role": "assistant", "content": content})
                spoken.append(content)
        except LLMError as e:
            del conv.history[start_len:]
            await emit({"type": "error", "text": str(e)})
            await emit({"type": "assistant_end", "id": msg_id, "text": ""})
            await emit({"type": "state", "state": "idle"})
            return ""
        except asyncio.CancelledError:
            self._repair_history()
            conv.add({"role": "assistant", "content": "(Vom Nutzer abgebrochen.)"})
            conv.save()
            await self.memory.log_exchange(user_text, "\n\n".join(spoken + ["(abgebrochen)"]), tool_notes)
            await emit({"type": "assistant_end", "id": msg_id, "text": "\n\n".join(spoken), "cancelled": True})
            await emit({"type": "state", "state": "idle"})
            raise

        answer = "\n\n".join(spoken)
        await emit({"type": "assistant_end", "id": msg_id, "text": answer})
        if self._plan and answer.strip():
            await emit({"type": "plan", "id": msg_id, "text": answer, "steps": plan_steps(answer)})
        # Antwort ist fertig – das Nachbereiten (Tagebuch, Verdichten per LLM) läuft still im Hintergrund
        await emit({"type": "state", "state": "idle"})
        conv.age_tool_results()  # lange Tool-Ergebnisse älterer Runden auf einen Auszug kürzen
        conv.save()
        await self.memory.log_exchange(user_text, answer, tool_notes)
        try:
            if await conv.compact(self.llm, self.history_budget()):
                await emit({"type": "memory", "text": "Älterer Gesprächsverlauf wurde ins Gedächtnis verdichtet."})
        except Exception as e:  # noqa: BLE001
            log.warning("Kompaktierung fehlgeschlagen: %s", e)
        return answer

    async def _step(self, hits, emit: Emit, msg_id: str, final: bool = False) -> tuple[str, list]:
        """Ein Modellschritt: streamt Tokens an die UI, liefert (Text, Tool-Aufrufe)."""
        filt = ThinkFilter()
        result: dict = {}
        await emit({"type": "state", "state": "thinking"})
        async for ev in self._stream_fitting(hits, final=final):
            if ev["type"] == "context":
                await emit(ev)
            elif ev["type"] == "token":
                text = filt.feed(ev["text"])
                thought = filt.take_thought()
                if thought:
                    await emit({"type": "reasoning", "id": msg_id, "text": thought})
                if text:
                    await emit({"type": "token", "id": msg_id, "text": text})
            elif ev["type"] == "reasoning":
                # Denkkette: nicht Teil der Antwort, wird nur angezeigt (Orb-Zoom) und nicht vorgelesen
                await emit({"type": "reasoning", "id": msg_id, "text": ev.get("text", "")})
            elif ev["type"] == "done":
                result = ev["message"]
                stats = ev.get("stats") or {}
                if stats.get("tps"):
                    await emit({"type": "llm_stats", **stats})
                total = prompt_size(stats, (self.last_context or {}).get("used", 0))
                if self.last_context is not None and total:
                    self.last_context["real"] = total
                    if stats.get("prompt_cached") is not None:
                        self.last_context["cached"] = stats["prompt_cached"]
                    self.learn_tokens(self.last_context.get("used", 0), total)
                    await emit({"type": "context", **self.last_context})
        tail = filt.flush()
        if tail:
            await emit({"type": "token", "id": msg_id, "text": tail})
        content = strip_think(result.get("content", ""))
        return content, ([] if final else result.get("tool_calls") or [])

    async def _stream_fitting(self, hits, final: bool = False):
        """Stream eines Schritts; passt der Prompt nicht ins Kontextfenster, einmal mit kleinerem Budget
        neu versuchen (der Server meldet das, bevor ein Token kommt). final=True: ohne Tools, mit der Bitte
        um eine Zwischenbilanz."""
        self._budget_scale = 1.0
        for attempt in range(3):
            try:
                messages = self.build_messages(hits)
                yield {"type": "context", **(self.last_context or {})}
                if final:
                    messages.append({"role": "user", "content": prompts.text(self.cfg, "final_nudge")})
                kwargs = {} if self._think is None else {"think": self._think}
                async for ev in self.llm.chat_stream(messages, None if final else self.schemas, **kwargs):
                    yield ev
                return
            except ContextOverflow as e:
                if attempt == 2:
                    raise
                ratio = (e.n_ctx - ANSWER_RESERVE) / e.n_prompt if e.n_ctx and e.n_prompt else 0.7
                self._budget_scale *= max(0.3, min(0.85, ratio * 0.9))
                log.warning("Kontext zu klein (%s/%s Token) – kürze Verlauf und versuche es erneut",
                            e.n_prompt, e.n_ctx)

    def _repair_history(self) -> None:
        """Nach Abbruch: offene Tool-Calls mit Platzhalter-Ergebnissen schließen."""
        hist = self.memory.conversation.history
        for i in range(len(hist) - 1, -1, -1):
            m = hist[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                answered = sum(1 for x in hist[i + 1:] if x["role"] == "tool")
                for call in m["tool_calls"][answered:]:
                    hist.append({"role": "tool", "content": "[abgebrochen]",
                                 "tool_name": call.get("function", {}).get("name", "")})
                break
            if m["role"] == "user":
                break

    async def _execute(self, call: dict, emit: Emit, confirm: Confirm) -> tuple[str, str, str]:
        fn = call.get("function", {})
        name = fn.get("name", "")
        call_id = uuid.uuid4().hex[:8]
        spec = get_tool(name)
        if spec and not spec.is_enabled(self.cfg):
            spec = None
        if not spec:
            return name, f"Unbekanntes Tool '{name}'. Verfügbar: {', '.join(n for n, t in self.tools.items() if t.is_enabled(self.cfg))}", f"{name}: unbekannt"
        args = coerce_args(spec, fn.get("arguments"))
        missing = missing_args(spec, args)
        if missing:
            return name, f"Fehlende Parameter: {', '.join(missing)}", f"{name}: Parameter fehlen"
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, emit=emit, call_id=call_id, services=self.services)
        risk, reason = spec.assess(ctx, args)
        if risk == SAFE and getattr(self, "_tainted", False) and spec.group in TAINT_GUARDED:
            risk, reason = CONFIRM, prompts.text(self.cfg, "tainted_confirm")
        if risk == SAFE and name == "run_shell" and not self.auto_read:
            risk, reason = CONFIRM, prompts.text(self.cfg, "auto_read_off")
        args_str = json.dumps(args, ensure_ascii=False)
        await emit({"type": "tool_call", "id": call_id, "name": name, "args": args, "risk": risk, "reason": reason,
                    "group": spec.group})
        if self._plan and risk != SAFE:  # Planmodus: Veränderndes nur vormerken, nicht ausführen, nicht nachfragen
            result = prompts.text(self.cfg, "plan_skipped")
            await emit({"type": "tool_result", "id": call_id, "status": "planned", "text": result})
            return name, result, f"{name} {args_str} → im Plan vorgemerkt"

        if risk == BLOCKED:
            result = f"BLOCKIERT ({reason}). Dieser Befehl wird aus Sicherheitsgründen nie ausgeführt."
            await emit({"type": "tool_result", "id": call_id, "status": "blocked", "text": result})
            return name, result, f"{name} {args_str} → blockiert ({reason})"
        edited: list[str] = []
        if risk == CONFIRM:
            await emit({"type": "state", "state": "confirm"})
            decision = await confirm(call_id, name, args, reason)
            changes = {}
            if isinstance(decision, tuple):  # (bestätigt, im Fenster bearbeitete Felder)
                decision, changes = decision[0], decision[1] or {}
            if not decision:
                result = "Der Nutzer hat die Ausführung abgelehnt."
                await emit({"type": "tool_result", "id": call_id, "status": "denied", "text": result})
                return name, result, f"{name} {args_str} → abgelehnt"
            # nur die freigegebenen Felder übernehmen (z. B. An/Betreff/Text einer Mail)
            for key in spec.editable:
                if key in changes and str(changes[key]) != str(args.get(key, "")):
                    args[key] = str(changes[key])[:50_000]
                    edited.append(key)
            if edited:
                args_str = json.dumps(args, ensure_ascii=False)

        await emit({"type": "state", "state": "executing", "tool": name})
        try:
            result = await spec.func(ctx, **args)
            status = "ok"
            if is_taint_source(name, result):
                self._tainted = True
        except asyncio.CancelledError:
            await emit({"type": "tool_result", "id": call_id, "status": "error", "text": "abgebrochen"})
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Tool %s fehlgeschlagen", name)
            result, status = f"Fehler: {e}", "error"
        if edited:  # das Modell soll wissen, was tatsächlich ausgeführt wurde
            result += "\n(Vom Nutzer vor dem Ausführen geändert: " + ", ".join(edited) + " – " + \
                json.dumps({k: args[k] for k in edited}, ensure_ascii=False)[:1500] + ")"
        await emit({"type": "tool_result", "id": call_id, "status": status, "text": clip(result, 3000)})
        first = result.strip().splitlines()[0] if result.strip() else ""
        return name, result, f"{name} {args_str} → {first[:160]}"
