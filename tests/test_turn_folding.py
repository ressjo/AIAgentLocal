"""Lange Aufgaben: ältere Schritte der laufenden Runde werden gefaltet statt am Kontextfenster zu scheitern –
ohne dass Inhalte verloren gehen (volle Fassung per earlier_output)."""

import json

from conftest import run

from orbwise.agent import Agent, is_taint_source
from orbwise.config import ProfileConfig
from orbwise.llm import ContextOverflow
from orbwise.memory.context import Conversation, digest, msg_tokens
from orbwise.tools.registry import ToolContext, get_tool, load_all_tools

LONG = "Exit-Code 0\n" + "\n".join(f"zeile {i} " + "x" * 60 for i in range(40)) + \
    "\nerror: Platte /dev/sdb fast voll\n" + "\n".join(f"mehr {i} " + "y" * 60 for i in range(40)) + "\nERGEBNIS 42"


def long_turn(steps=20, write_arg=False):
    conv = Conversation()
    conv.add({"role": "user", "content": "Alte Frage"})
    conv.add({"role": "assistant", "content": "Alte Antwort"})
    conv.add({"role": "user", "content": "Räum das System auf"})
    for i in range(steps):
        args = {"path": f"/tmp/f{i}", "content": "z" * 2000} if write_arg else {"command": f"du -sh /var/{i}"}
        conv.add({"role": "assistant", "content": f"Ich prüfe Teil {i}.",
                  "tool_calls": [{"function": {"name": "write_file" if write_arg else "run_shell", "arguments": args}}]})
        conv.add({"role": "tool", "content": LONG if i % 5 else "kurz: ok", "tool_name": "run_shell"})
    return conv


def test_fold_frees_space_and_keeps_structure_and_content():
    conv = long_turn()
    original = json.loads(json.dumps(conv.history))
    budget = int(conv.turn_tokens() / 0.9)
    folded, freed = conv.fold_current_turn(budget)
    assert folded > 0 and freed > 0 and conv.turn_tokens() <= budget * 0.5 + 200
    assert len(conv.history) == len(original)  # nichts entfernt, Paare Aufruf/Ergebnis bleiben vollständig
    assert [m["role"] for m in conv.history] == [m["role"] for m in original]
    assert conv.history[:4] == original[:4]  # alte Runde, Frage und erster Text unverändert
    assert conv.history[-4:] == original[-4:]  # die letzten 2 Schritte bleiben wörtlich
    for new, old in zip(conv.history, original):
        if new["role"] == "assistant":
            assert new["content"] == old["content"]  # eigene Texte des Modells bleiben
        if old["role"] == "tool" and len(old["content"]) < 400:
            assert new == old  # kurze Ergebnisse bleiben
    folded_msg = next(m for m in conv.history if m.get("folded"))
    text = folded_msg["content"]
    assert "Exit-Code 0" in text and "error: Platte /dev/sdb fast voll" in text and "ERGEBNIS 42" in text
    entry = conv.stash[folded_msg["folded"]]
    assert entry["text"] == LONG and entry["call"].startswith("run_shell") and entry["tool"] == "run_shell"
    # Hysterese: ohne neue Schritte bleibt alles, wie es ist (stabiler Prompt-Anfang)
    snapshot = json.dumps(conv.history)
    assert conv.fold_current_turn(budget) == (0, 0) and json.dumps(conv.history) == snapshot


def test_nothing_happens_while_it_fits():
    conv = long_turn(steps=4)
    snapshot = json.dumps(conv.history)
    assert conv.fold_current_turn(conv.turn_tokens() * 2) == (0, 0)
    assert json.dumps(conv.history) == snapshot and not conv.stash


def test_long_arguments_are_shortened_only_in_folded_steps():
    conv = long_turn(write_arg=True)
    conv.fold_current_turn(int(conv.turn_tokens() / 0.9))
    calls = [m["tool_calls"][0]["function"]["arguments"]["content"] for m in conv.history if m.get("tool_calls")]
    assert calls[0] == "[2000 Zeichen]" and calls[-1] == "z" * 2000 and calls[-2] == "z" * 2000
    entry = conv.stash["1"]
    assert '"content": "' + "z" * 2000 in entry["call"]  # volle Argumente bleiben abrufbar


def test_earlier_output_returns_the_full_original(cfg, memory):
    conv = memory.conversation
    for m in long_turn().history:
        conv.add(m)
    conv.fold_current_turn(int(conv.turn_tokens() / 0.9))
    load_all_tools()
    ctx = ToolContext(cfg=cfg, memory=memory)
    out = run(get_tool("earlier_output").func(ctx, step=1))
    assert out.startswith("[Schritt 1 · run_shell] run_shell") and out.endswith(LONG)
    assert "Keine gefaltete Ausgabe" in run(get_tool("earlier_output").func(ctx, step=99))
    # zurückgeholte Mail-/Bildschirminhalte bleiben „unsicher“
    assert is_taint_source("earlier_output", "[Schritt 3 · mail_read] mail_read {}\nHallo")
    assert not is_taint_source("earlier_output", out)


def test_digest_is_short_and_points_to_the_full_text():
    d = digest(LONG, "7")
    assert len(d) < 700 and "earlier_output(step=7)" in d and f"{len(LONG)} Zeichen" in d


def test_stash_is_saved_with_the_chat(tmp_path):
    conv = long_turn()
    conv.state_path = tmp_path / "chat.json"
    conv.fold_current_turn(int(conv.turn_tokens() / 0.9))
    conv.save()
    again = Conversation(tmp_path / "chat.json")
    assert again.stash == conv.stash and again.stash


# ---------------------------------------------------------------- Agent: lange Aufgabe mit kleinem Fenster


class Worker:
    """Liest Datei um Datei (lange Ausgaben), bis `steps` erledigt sind – mit hartem Kontextlimit wie llama-server."""

    profile = ProfileConfig(backend="openai", base_url="http://x/v1", model="klein", num_ctx=16384)
    context_size = 16384  # wie beim Nutzer: 16k

    def __init__(self, path, steps=20, limit=16384):
        self.path, self.steps, self.limit = path, steps, limit
        self.prompts: list[int] = []
        self.overflows = 0

    async def chat_stream(self, messages, tools=None, think=None):
        size = sum(msg_tokens(m) for m in messages) + len(json.dumps(tools or [])) // 3
        self.prompts.append(size)
        if size > self.limit:
            self.overflows += 1
            raise ContextOverflow("zu groß", n_ctx=self.limit, n_prompt=size)
        done = sum(1 for m in messages if m["role"] == "tool")  # Testverlauf hat nur diese eine Runde
        if tools is None:  # Zwischenbilanz
            msg = {"role": "assistant", "content": f"Zwischenstand: {done} Dateien gelesen."}
        elif done < self.steps:
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"function": {"name": "read_file",
                                                "arguments": {"path": str(self.path / f"log{done}.txt")}}}]}
        else:
            msg = {"role": "assistant", "content": f"Fertig: {done} Dateien gelesen."}
        if msg["content"]:
            yield {"type": "token", "text": msg["content"]}
        yield {"type": "done", "message": msg, "stats": {}}

    async def chat(self, messages):
        return "Zusammenfassung"


def big_file(tmp_path):
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

    answer = run(agent.run(text, emit, confirm))
    return answer, events


def test_long_task_finishes_in_a_small_window(cfg, memory, tmp_path):
    cfg.tools.max_steps = 25
    llm = Worker(big_file(tmp_path), steps=24)  # ohne Falten: 24 × ~1.100 Token – weit über 16k
    answer, events = run_agent(Agent(cfg, llm, memory), "Lies alle Logs")
    assert answer == "Fertig: 24 Dateien gelesen." and not any(e["type"] == "error" for e in events)
    assert llm.overflows == 0 and max(llm.prompts) <= 16384 - 1500
    folds = [e for e in events if e.get("phase") == "fold"]
    assert folds and folds[0]["freed"] > 0
    assert memory.conversation.stash  # alles abrufbar


def test_overflow_ends_with_a_summary_instead_of_losing_the_work(cfg, memory, tmp_path, monkeypatch):
    cfg.tools.max_steps = 25
    real_fold = Conversation.fold_current_turn

    def only_emergency(self, budget, **kw):  # normales Falten „reicht nicht“ – erzwingt den Notfall
        return real_fold(self, budget, **kw) if kw.get("trigger") == 0 else (0, 0)

    monkeypatch.setattr(Conversation, "fold_current_turn", only_emergency)
    llm = Worker(big_file(tmp_path), steps=40, limit=12000)  # Server kleiner als eingestellt
    agent = Agent(cfg, llm, memory)
    answer, events = run_agent(agent, "Lies alle Logs")
    assert llm.overflows > 0 and not any(e["type"] == "error" for e in events)
    assert answer.startswith("Zwischenstand:") and "mach weiter" in answer
    hist = memory.conversation.history
    assert sum(1 for m in hist if m["role"] == "tool") > 3  # erledigte Schritte sind noch da
    assert any(e.get("phase") == "done" and e.get("error") for e in events)  # Zeile in der Aktivität endet


def test_hopeless_overflow_keeps_the_steps(cfg, memory, tmp_path):
    class Shrinking(Worker):
        async def chat_stream(self, messages, tools=None, think=None):
            if self.prompts and tools is not None or tools is None and self.prompts:
                self.limit = 10  # nach dem ersten Schritt passt gar nichts mehr
            async for ev in super().chat_stream(messages, tools, think):
                yield ev

    llm = Shrinking(big_file(tmp_path), steps=5)
    answer, events = run_agent(Agent(cfg, llm, memory), "Lies alle Logs")
    errors = [e for e in events if e["type"] == "error"]
    assert errors and answer == ""
    hist = memory.conversation.history
    assert hist[-1]["role"] == "assistant" and "Unterbrochen" in hist[-1]["content"]
    assert any(m["role"] == "tool" for m in hist)  # nicht weggeworfen


def test_thinking_reserves_more_room(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    normal = agent.context_budget()
    agent._think = True
    assert agent.context_budget() < normal


def test_normal_prompt_is_unchanged_until_something_is_folded(cfg, llm, memory):
    agent = Agent(cfg, llm, memory)
    agent.choose_tools()
    names = {s["function"]["name"] for s in agent.schemas}
    assert "earlier_output" not in names and "earlier_output" not in str(agent.all_schemas)
    memory.conversation.stash["1"] = {"tool": "run_shell", "call": "", "text": "x"}
    agent.choose_tools()
    assert "earlier_output" in {s["function"]["name"] for s in agent.schemas}
