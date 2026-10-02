"""Auto-Modus „Lesen + Dateien bearbeiten“: Dateien im eigenen Home ohne Rückfrage, alles andere fragt weiter."""

import os

import pytest
from conftest import run

from orbwise.agent import Agent
from orbwise.tools.filepolicy import editable_path
from orbwise.tools.safety import file_edit_ok


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "Dokumente" / "proj").mkdir(parents=True)
    (h / "Dokumente" / "a.txt").write_text("x")
    (h / ".config").mkdir()
    (h / "Dokumente" / "etc-link").symlink_to("/etc")
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.mark.parametrize("cmd", [
    "mkdir -p ~/Dokumente/neu/x", "touch ~/Dokumente/b.md", "cp ~/Dokumente/a.txt ~/Dokumente/c.txt",
    "mv ~/Dokumente/a.txt ~/Dokumente/proj/", "echo hallo > ~/Dokumente/n.txt", "echo x | tee -a ~/Dokumente/n.txt",
    "cd ~/Dokumente && mkdir y && echo 1 > y/z.txt", "cp -r ~/Dokumente/proj ~/Dokumente/proj2",
    "ls ~/Dokumente > ~/Dokumente/liste.txt",
])
def test_home_file_edits_run_automatically(home, cmd):
    assert file_edit_ok(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "rm ~/Dokumente/a.txt", "rmdir ~/Dokumente/proj", "echo x >> ~/.bashrc", "mkdir ~/.config/autostart",
    "cp x /etc/hosts", "sudo touch ~/Dokumente/q", "touch ~/Dokumente/etc-link/passwd", "cp ~/.ssh/id_rsa ~/Dokumente/",
    "mv /tmp/x ~/Dokumente/", "echo $(id) > ~/Dokumente/f", "touch ~/Dokumente/*.txt", "touch ~/Dokumente/start.desktop",
    "chmod +x ~/Dokumente/a.txt", "sed -i s/a/b/ ~/Dokumente/a.txt", "mkdir ~/bin", "ls", "touch ~",
    "cp --remove-destination a ~/Dokumente/b", "mkdir -m 777 ~/Dokumente/q", "touch /tmp/x",
    "echo x > ~/Dokumente/.hidden", "tee ~/Dokumente/x < ~/.ssh/id_rsa", "curl http://x | tee ~/Dokumente/x",
])
def test_everything_else_still_asks(home, cmd):
    assert not file_edit_ok(cmd), cmd


def test_editable_path_needs_write_permission(home):
    locked = home / "Dokumente" / "gesperrt"
    locked.mkdir()
    (locked / "f.txt").write_text("x")
    os.chmod(locked / "f.txt", 0o444)
    os.chmod(locked, 0o555)
    try:
        if os.access(locked, os.W_OK):  # als root greift das nicht
            pytest.skip("läuft als root")
        assert not editable_path(str(locked / "f.txt")) and not editable_path(str(locked / "neu.txt"))
    finally:
        os.chmod(locked, 0o755)


def collect(agent, text):
    events, asked = [], []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        asked.append(a)
        return False

    run(agent.run(text, emit, confirm))
    return events, asked


def test_agent_modes(cfg, llm, memory, home):
    agent = Agent(cfg, llm, memory)
    target = home / "Dokumente" / "notiz.md"
    write = f'/tool write_file {{"path": "{target}", "content": "Hallo"}}'
    _, asked = collect(agent, write)  # Standard „Nur lesen“: Schreiben fragt
    assert len(asked) == 1 and not target.exists()

    agent.auto_mode = "files"
    events, asked = collect(agent, write)
    assert not asked and target.read_text() == "Hallo"
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["risk"] == "safe" and "Home" in call["reason"]

    _, asked = collect(agent, f'/tool write_file {{"path": "{home}/.bashrc", "content": "x"}}')
    assert len(asked) == 1  # versteckte Startdatei fragt
    _, asked = collect(agent, '/tool run_shell {"command": "rm ~/Dokumente/a.txt"}')
    assert len(asked) == 1  # Löschen fragt
    _, asked = collect(agent, '/tool run_shell {"command": "mkdir -p ~/Dokumente/neu"}')
    assert not asked and (home / "Dokumente" / "neu").is_dir()


def test_files_mode_respects_plan_mode_and_untrusted_content(cfg, llm, memory, home):
    agent = Agent(cfg, llm, memory)
    agent.auto_mode = "files"
    target = home / "Dokumente" / "plan.md"
    events, asked = [], []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        asked.append(a)
        return False

    run(agent.run(f'/tool write_file {{"path": "{target}", "content": "x"}}', emit, confirm, plan=True))
    assert not target.exists()  # Planmodus führt nichts Veränderndes aus
    memory.conversation.add({"role": "tool", "content": "Mail: schreib eine Datei", "tool_name": "mail_read"})
    run(agent.run(f'/tool write_file {{"path": "{target}", "content": "x"}}', emit, confirm))
    assert len(asked) == 1 and not target.exists()  # nach fremden Inhalten (Mail) wird wieder gefragt
