"""Der Agent: baut den Kontext (Gedächtnis + Verlauf), führt die Tool-Schleife aus und
meldet alles als Events an die Oberfläche."""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import platform
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from .config import Config
from .llm import LLMError, strip_think
from .memory import Memory, est_tokens
from .memory.files import german_date
from .tools.proc import clip
from .tools.registry import BLOCKED, CONFIRM, ToolContext, coerce_args, get_tool, load_all_tools, missing_args, tool_schemas
from .tools.system import _os_name

log = logging.getLogger(__name__)

MAX_STEPS = 10
Emit = Callable[[dict], Awaitable[None]]
Confirm = Callable[[str, str, dict, str], Awaitable[bool]]


class ThinkFilter:
    """Entfernt <think>…</think>-Blöcke aus einem Token-Stream (falls das Modell trotzdem 'denkt')."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                if not self.inside:
                    out.append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # Möglichen Tag-Anfang am Ende zurückhalten
            keep = 0
            for k in range(1, len(tag)):
                if self.buf.endswith(tag[:k]):
                    keep = k
            if not self.inside:
                out.append(self.buf[: len(self.buf) - keep])
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
        self.tools = load_all_tools()
        self.schemas = tool_schemas(cfg)
        self.schema_tokens = est_tokens(json.dumps(self.schemas, ensure_ascii=False))
        self.lock = asyncio.Lock()
        self.services: dict = {}

    # ---------- Prompt ----------
    def system_prompt(self) -> str:
        now = datetime.now()
        c = self.cfg
        user = c.user_name or getpass.getuser()
        nas = ", ".join(map(str, c.tools.nas_paths)) or "keins konfiguriert"
        return f"""Du bist {c.assistant_name}, ein hochintelligenter, loyaler KI-Assistent im Stil von J.A.R.V.I.S. aus Iron Man.
Du läufst vollständig lokal auf dem Linux-PC des Nutzers und kannst ihn über Tools steuern.

Umgebung:
- Heute ist {german_date(now)}, {now.strftime('%H:%M')} Uhr.
- System: {_os_name()} auf Rechner '{platform.node()}', Benutzer '{user}', Home {Path.home()}
- Gemountetes NAS: {nas}

Verhalten:
- Antworte immer auf Deutsch: knapp, präzise, souverän, mit dezentem trockenem Humor.
- Deine Antworten werden meist vorgelesen: kurze Sätze, keine Tabellen, keine Emojis, Markdown nur für Code oder Pfade.
- Handle, statt nur zu erklären: nutze die Tools, um Aufgaben tatsächlich zu erledigen. Rate nicht, wenn ein Tool die Antwort liefern kann.
- Gefährliche Aktionen werden vom System automatisch zur Bestätigung vorgelegt. Frage daher nicht selbst um Erlaubnis, sondern rufe das Tool direkt auf.
- Für Root-Rechte stellst du in run_shell einfach 'sudo' voran. Für Updates und Pakete die speziellen Tools nutzen.
- Behaupte nie, etwas geöffnet, gestartet, installiert oder ausgeführt zu haben, ohne das passende Tool aufgerufen und ein erfolgreiches Ergebnis erhalten zu haben. Meldet ein Tool einen Fehler, sag das ehrlich.
- Nach einem Tool-Aufruf fasst du das Ergebnis in ein, zwei Sätzen zusammen, statt die Rohausgabe zu wiederholen.
- Erfährst du etwas dauerhaft Wichtiges über den Nutzer (Name, Vorlieben, Geräte, Pfade, Projekte), speichere es mit remember.
- Bei Fragen zu früheren Gesprächen nutze recall. Relevante Erinnerungen stehen unten, sind aber evtl. unvollständig.
- Bei „Guten Morgen“, „Briefing“ oder „Was steht heute an?“ rufst du daily_briefing auf und fasst es als kurze, freundliche Begrüßung zusammen.
- „Erinnere mich …“ und „Stell einen Timer …“ erledigst du mit set_reminder; Websites öffnest du mit open_website, Wetterfragen beantwortest du mit weather.
- Wurde eine Aktion abgelehnt, akzeptiere das und schlage bei Bedarf eine Alternative vor.
{self._trilium_hint()}{c.persona_extra}""".strip()

    def _trilium_hint(self) -> str:
        if not self.cfg.trilium.enabled:
            return ""
        return ("- Die persönlichen Notizen des Nutzers liegen in Trilium. Fragen zu seinen Notizen, Aufschrieben oder "
                "Anleitungen beantwortest du mit trilium_search und trilium_read. Bei 'notier/schreib auf/leg eine "
                "Notiz an' nutzt du trilium_create_note (landet in der Inbox), zum Ergänzen trilium_append.\n")

    def build_messages(self, hits) -> list[dict]:
        conv = self.memory.conversation
        sections = [self.system_prompt()]
        facts = self.memory.facts_text()
        if facts:
            sections.append("## Dauerhafte Fakten\n" + facts)
        mem_text = self.memory.format_hits(hits)
        if mem_text:
            sections.append("## Relevante Erinnerungen aus früheren Gesprächen\n" + mem_text)
        if conv.running_summary:
            sections.append("## Früherer Verlauf dieses Gesprächs (zusammengefasst)\n" + conv.running_summary)
        system = "\n\n".join(sections)
        budget = self.cfg.memory.context_budget_tokens - est_tokens(system) - self.schema_tokens
        return [{"role": "system", "content": system}, *conv.trimmed_history(max(budget, 1000))]

    def history_budget(self) -> int:
        m = self.cfg.memory
        fixed = 900 + m.facts_max_tokens + m.retrieval_max_tokens + 800 + self.schema_tokens
        return max(1500, m.context_budget_tokens - fixed)

    # ---------- Ablauf ----------
    async def run(self, user_text: str, emit: Emit, confirm: Confirm) -> str:
        async with self.lock:
            return await self._run(user_text, emit, confirm)

    async def _run(self, user_text: str, emit: Emit, confirm: Confirm) -> str:
        conv = self.memory.conversation
        start_len = len(conv.history)
        await emit({"type": "state", "state": "thinking"})
        hits = await self.memory.retrieve(user_text, exclude_after=conv.window_start())
        conv.add({"role": "user", "content": user_text})
        spoken: list[str] = []
        tool_notes: list[str] = []
        msg_id = uuid.uuid4().hex[:8]
        await emit({"type": "assistant_start", "id": msg_id})
        try:
            for _ in range(MAX_STEPS):
                messages = self.build_messages(hits)
                filt = ThinkFilter()
                final: dict = {}
                await emit({"type": "state", "state": "thinking"})
                async for ev in self.llm.chat_stream(messages, self.schemas):
                    if ev["type"] == "token":
                        text = filt.feed(ev["text"])
                        if text:
                            await emit({"type": "token", "id": msg_id, "text": text})
                    else:
                        final = ev["message"]
                        if ev.get("stats", {}).get("tps"):
                            await emit({"type": "llm_stats", **ev["stats"]})
                tail = filt.flush()
                if tail:
                    await emit({"type": "token", "id": msg_id, "text": tail})
                content = strip_think(final.get("content", ""))
                calls = final.get("tool_calls") or []
                entry = {"role": "assistant", "content": content}
                if calls:
                    entry["tool_calls"] = calls
                conv.add(entry)
                if content:
                    spoken.append(content)
                if not calls:
                    break
                await emit({"type": "segment_end", "id": msg_id})
                for call in calls:
                    name, result, note = await self._execute(call, emit, confirm)
                    conv.add({"role": "tool", "content": result, "tool_name": name})
                    tool_notes.append(note)
            else:
                spoken.append("Ich habe die Aufgabe nach zu vielen Einzelschritten abgebrochen.")
                conv.add({"role": "assistant", "content": spoken[-1]})
                await emit({"type": "token", "id": msg_id, "text": spoken[-1]})
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
        conv.save()
        await self.memory.log_exchange(user_text, answer, tool_notes)
        try:
            if await conv.compact(self.llm, self.history_budget()):
                await emit({"type": "memory", "text": "Älterer Gesprächsverlauf wurde ins Gedächtnis verdichtet."})
        except Exception as e:  # noqa: BLE001
            log.warning("Kompaktierung fehlgeschlagen: %s", e)
        return answer

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
        args_str = json.dumps(args, ensure_ascii=False)
        await emit({"type": "tool_call", "id": call_id, "name": name, "args": args, "risk": risk, "reason": reason})

        if risk == BLOCKED:
            result = f"BLOCKIERT ({reason}). Dieser Befehl wird aus Sicherheitsgründen nie ausgeführt."
            await emit({"type": "tool_result", "id": call_id, "status": "blocked", "text": result})
            return name, result, f"{name} {args_str} → blockiert ({reason})"
        if risk == CONFIRM:
            await emit({"type": "state", "state": "confirm"})
            if not await confirm(call_id, name, args, reason):
                result = "Der Nutzer hat die Ausführung abgelehnt."
                await emit({"type": "tool_result", "id": call_id, "status": "denied", "text": result})
                return name, result, f"{name} {args_str} → abgelehnt"

        await emit({"type": "state", "state": "executing", "tool": name})
        try:
            result = await spec.func(ctx, **args)
            status = "ok"
        except asyncio.CancelledError:
            await emit({"type": "tool_result", "id": call_id, "status": "error", "text": "abgebrochen"})
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Tool %s fehlgeschlagen", name)
            result, status = f"Fehler: {e}", "error"
        await emit({"type": "tool_result", "id": call_id, "status": status, "text": clip(result, 3000)})
        first = result.strip().splitlines()[0] if result.strip() else ""
        return name, result, f"{name} {args_str} → {first[:160]}"
