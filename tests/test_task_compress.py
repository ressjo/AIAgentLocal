"""Lange Aufgaben: bei 90 % des Kontextfensters einmal pausieren und komprimieren, dann weitermachen.

Wichtig für langsame Grafikkarten: zwischen den Komprimierungen wächst der Prompt nur hinten an (der Server kann
seinen Cache weiterverwenden) – es wird nicht Schritt für Schritt umgebaut."""

import json

from conftest import run

from orbwise.agent import Agent
from orbwise.config import ProfileConfig
from orbwise.llm import ContextOverflow
from orbwise.memory.context import Conversation, msg_tokens, render_task


class Worker:
    """Liest Datei um Datei (lange Ausgaben), bis `steps` Aufrufe erledigt sind; hartes Limit wie llama-server."""

    profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="klein", num_ctx=16384)
    context_size = 16384

    def __init__(self, path, steps=24, limit=16384):
        self.path, self.steps, self.limit = path, steps, limit
        self.prompts: list[list[dict]] = []
        self.sizes: list[int] = []
        self.summaries: list[str] = []
        self.overflows = self.done = 0

    async def chat_stream(self, messages, tools=None, think=None):
        size = sum(msg_tokens(m) for m in messages) + len(json.dumps(tools or [])) // 3
        if size > self.limit:
            self.overflows += 1
            raise ContextOverflow("zu groß", n_ctx=self.limit, n_prompt=size)
        self.prompts.append(messages)
        self.sizes.append(size)
        if tools is None:
            msg = {"role": "assistant", "content": f"Zwischenstand: {self.done} Dateien gelesen."}
        elif self.done < self.steps:
            msg = {"role": "assistant", "content": "", "tool_calls": [{"function": {
                "name": "read_file", "arguments": {"path": str(self.path / f"log{self.done}.txt")}}}]}
            self.done += 1
        else:
            msg = {"role": "assistant", "content": f"Fertig: {self.done} Dateien gelesen."}
        if msg["content"]:
            yield {"type": "token", "text": msg["content"]}
        yield {"type": "done", "message": msg, "stats": {}}

    async def chat(self, messages):  # Zusammenfassung des Arbeitsstands
        self.summaries.append(messages[-1]["content"])
        return f"Erledigt: {self.done} Logs gelesen, Platte /dev/sdb fast voll.\nOffen: restliche Logs."


def logs(tmp_path):
    for n in range(40):
        (tmp_path / f"log{n}.txt").write_text("\n".join(f"Log {n} Zeile {i}: " + "daten " * 12 for i in range(60)),
                                              encoding="utf-8")
    return tmp_path


def run_agent(agent, text):
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return True

    return run(agent.run(text, emit, confirm)), events


def is_prefix(prev: list[dict], cur: list[dict]) -> bool:
    return len(prev) <= len(cur) and cur[:len(prev)] == prev


def test_long_task_compresses_once_and_otherwise_only_appends(cfg, memory, tmp_path):
    cfg.tools.max_steps = 30
    llm = Worker(logs(tmp_path), steps=24)  # ohne Komprimieren: 24 × ~1.100 Token – weit über 16k
    answer, events = run_agent(Agent(cfg, llm, memory), "Lies alle Logs")
    assert answer == "Fertig: 24 Dateien gelesen." and not any(e["type"] == "error" for e in events)
    assert llm.overflows == 0
    compress = [e for e in events if e.get("phase") == "compress"]
    assert 1 <= len(compress) <= 24 // 5 and compress[0]["percent"] >= 80  # selten, nicht pro Schritt
    done = next(e for e in events if e.get("compress"))
    assert done["after"] < done["before"] / 2
    # zwischen den Komprimierungen bleibt der Prompt-Anfang gleich (Cache des Servers bleibt gültig)
    breaks = sum(1 for a, b in zip(llm.prompts, llm.prompts[1:]) if not is_prefix(a, b))
    assert breaks == len(compress)
    assert max(llm.sizes) <= 0.9 * 16384 + 1200  # nie voll gelaufen
    # Arbeitsstand steht in der Notiz, die Schritte sind gespeichert, aber nicht mehr im Prompt
    after = llm.prompts[-1]
    user = [m for m in after if m["role"] == "user"][-1]["content"]
    assert "Zwischenstand dieser Aufgabe" in user and "/dev/sdb fast voll" in user
    hist = memory.conversation.history
    assert sum(1 for m in hist if m["role"] == "tool") == 24 and any(m.get("packed") for m in hist)
    assert "Lies alle Logs" in llm.summaries[0] and "Log 0 Zeile 0" in llm.summaries[0]


def test_short_tasks_are_not_touched(cfg, memory, tmp_path):
    llm = Worker(logs(tmp_path), steps=3)
    answer, events = run_agent(Agent(cfg, llm, memory), "Lies drei Logs")
    assert answer.startswith("Fertig") and not any(e.get("phase") == "compress" for e in events)
    assert all(is_prefix(a, b) for a, b in zip(llm.prompts, llm.prompts[1:])) and not llm.summaries


def test_server_overflow_compresses_and_continues(cfg, memory, tmp_path):
    cfg.tools.max_steps = 30
    llm = Worker(logs(tmp_path), steps=14, limit=11000)  # Server kleiner als eingestellt
    answer, events = run_agent(Agent(cfg, llm, memory), "Lies alle Logs")
    assert llm.overflows >= 1 and answer == "Fertig: 14 Dateien gelesen."
    assert any(e.get("phase") == "compress" for e in events) and not any(e["type"] == "error" for e in events)


def test_hopeless_overflow_keeps_the_steps(cfg, memory, tmp_path):
    class Shrinking(Worker):
        async def chat_stream(self, messages, tools=None, think=None):
            if self.done >= 3:
                self.limit = 10  # ab jetzt passt gar nichts mehr
            async for ev in super().chat_stream(messages, tools, think):
                yield ev

    llm = Shrinking(logs(tmp_path), steps=8)
    answer, events = run_agent(Agent(cfg, llm, memory), "Lies alle Logs")
    assert any(e["type"] == "error" for e in events) and answer == ""
    hist = memory.conversation.history
    assert hist[-1]["role"] == "assistant" and "Unterbrochen" in hist[-1]["content"]
    assert sum(1 for m in hist if m["role"] == "tool") == 3  # erledigte Schritte bleiben


def test_task_note_belongs_to_one_task_and_is_saved(tmp_path):
    conv = Conversation(tmp_path / "c.json")
    conv.add({"role": "user", "content": "A"})
    conv.add({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "x", "arguments": {}}}]})
    conv.add({"role": "tool", "content": "lang " * 100, "tool_name": "x"})
    assert conv.pack_turn("Erledigt: x") == 2 and conv.turn_steps() == []
    assert [m["role"] for m in conv.trimmed_history(5000)] == ["user"]
    conv.save()
    again = Conversation(tmp_path / "c.json")
    assert again.task_note == "Erledigt: x" and len(again.history) == 3
    again.add({"role": "user", "content": "B"})
    assert again.task_note == ""


def test_render_task_keeps_head_and_tail_within_budget():
    steps = [{"role": "assistant", "content": "Ich schaue nach.",
              "tool_calls": [{"function": {"name": "run_shell", "arguments": {"command": "df -h"}}}]},
             {"role": "tool", "content": "ANFANG " + "x" * 9000 + " ENDE", "tool_name": "run_shell"}]
    text = render_task(steps, max_chars=2000)
    assert len(text) <= 2100 and "ANFANG" in text and "ENDE" in text and "df -h" in text


def test_thinking_reserves_more_room(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    normal = agent.context_budget()
    agent._think = True
    assert agent.context_budget() < normal
