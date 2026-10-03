"""edit_file (Ausschnitt ersetzen statt Datei neu schreiben) und die Aufgabenliste todo_write."""

import json

import pytest
from conftest import run

from orbwise.agent import Agent
from orbwise.tools.registry import ToolContext, get_tool, load_all_tools
from orbwise.tools.todo_tools import parse_todos


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def edit(cfg, **args):
    load_all_tools()
    return run(get_tool("edit_file").func(ToolContext(cfg=cfg, memory=None), **args))


def test_edit_replaces_exactly_one_snippet_and_shows_context(cfg, home):
    f = home / "proj" / "app.py"
    f.write_text("def a():\n    return 1\n\n\ndef b():\n    return 2\n", encoding="utf-8")
    out = edit(cfg, path=str(f), old_text="    return 2", new_text="    return 3")
    assert f.read_text() == "def a():\n    return 1\n\n\ndef b():\n    return 3\n"
    assert "Zeilen 6–6" in out and "return 3" in out and "def b" in out  # Kontext – kein Neulesen nötig


def test_edit_errors_help_the_model(cfg, home):
    f = home / "proj" / "app.py"
    f.write_text("x = 1\nx = 1\nprint(x)\n", encoding="utf-8")
    assert "2-mal" in edit(cfg, path=str(f), old_text="x = 1", new_text="x = 2")
    out = edit(cfg, path=str(f), old_text="print( x )", new_text="")
    assert "nicht vor" in out and "print(x)" in out  # ähnlichste Zeile als Hinweis
    assert "gibt es nicht" in edit(cfg, path=str(home / "fehlt.py"), old_text="a", new_text="b")
    edit(cfg, path=str(f), old_text="x = 1", new_text="x = 2", replace_all=True)
    assert f.read_text() == "x = 2\nx = 2\nprint(x)\n"
    assert f.read_text().count("x = 2") == 2


def collect(agent, text):
    asked = []

    async def emit(ev):
        pass

    async def confirm(*a):
        asked.append(a)
        return True

    run(agent.run(text, emit, confirm))
    return asked


def test_edit_follows_auto_modes(cfg, llm, memory, home):
    f = home / "proj" / "notiz.md"
    f.write_text("Hallo Welt\n", encoding="utf-8")
    agent = Agent(cfg, llm, memory)
    call = "/tool edit_file " + json.dumps({"path": str(f), "old_text": "Welt", "new_text": "Jarvis"})
    assert len(collect(agent, call)) == 1  # Standard: fragt
    agent.auto_mode = "files"
    f.write_text("Hallo Welt\n", encoding="utf-8")
    assert not collect(agent, call) and f.read_text() == "Hallo Jarvis\n"
    rc = home / ".bashrc"
    rc.write_text("alias a=b\n")
    for mode in ("files", "auto"):
        agent.auto_mode = mode
        hidden = "/tool edit_file " + json.dumps({"path": str(rc), "old_text": "a=b", "new_text": "a=c"})
        assert len(collect(agent, hidden)) == 1  # Startdatei fragt weiter


def test_todo_list_lives_in_the_answer_and_survives_compaction(cfg, llm, memory):
    assert [t["state"] for t in parse_todos("- [x] lesen\n- [~] ändern\n- [ ] testen\nnoch was")] == \
        ["done", "active", "open", "open"]
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    todos = "- [x] Dateien lesen\\n- [~] Fehler beheben\\n- [ ] Tests ausführen"
    async def no(*a):
        return False

    run(agent.run('/tool todo_write {"todos": "' + todos + '"}', emit, no))
    ev = next(e for e in events if e["type"] == "todos")
    assert [i["state"] for i in ev["items"]] == ["done", "active", "open"]
    assert memory.conversation.meta["todos"][1]["text"] == "Fehler beheben"
    appendix = agent._compact_appendix(memory.conversation.epoch_messages())
    assert "Aufgabenliste" in appendix and "- [~] Fehler beheben" in appendix
    assert "todo_write" in {s["function"]["name"] for s in agent.schemas}
    memory.switch_mode("coding")
    agent.choose_tools()
    assert {"todo_write", "edit_file"} <= {s["function"]["name"] for s in agent.schemas}
