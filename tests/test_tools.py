from jarvis.tools.apps import App, match_app
from jarvis.tools.registry import coerce_args, get_tool, load_all_tools
from jarvis.voice.tts import SentenceSplitter, clean_for_speech
from jarvis.server import check_host, parse_yes_no


def test_schemas_valid():
    tools = load_all_tools()
    for spec in tools.values():
        s = spec.schema()["function"]
        assert s["description"]
        assert set(s["parameters"]["required"]) <= set(s["parameters"]["properties"])


def test_coerce_args():
    spec = get_tool("find_files")
    args = coerce_args(spec, '{"query": "x", "max_results": "5", "bogus": 1}')
    assert args == {"query": "x", "max_results": 5}


def test_match_app():
    apps = [App("firefox.desktop", "Firefox", "Webbrowser", "Internet;WWW", "firefox %u", None),
            App("org.kde.dolphin.desktop", "Dolphin", "Dateiverwaltung", "files;Dateimanager", "dolphin", None)]
    assert match_app("firefox", apps).name == "Firefox"
    assert match_app("dateimanager", apps).name == "Dolphin"
    assert match_app("völlig unbekannt", apps) is None


def test_sentence_splitter_skips_code():
    sp = SentenceSplitter()
    out = []
    for tok in ["Ich habe das Update erfolgreich ", "ausgeführt. Hier der Befehl:\n```bash\nsudo ", "pacman -Syu\n```\nAlles ", "erledigt."]:
        out += sp.feed(tok)
    out += sp.flush()
    assert out[0] == "Ich habe das Update erfolgreich ausgeführt."
    assert not any("pacman" in s for s in out)
    assert out[-1] == "Alles erledigt."
    assert clean_for_speech("**Fertig** siehe https://x.de") == "Fertig siehe Link"


def test_yes_no():
    assert parse_yes_no("Ja, mach das") is True
    assert parse_yes_no("Nein, lieber nicht") is False
    assert parse_yes_no("Bestätigt") is True
    assert parse_yes_no("Wie spät ist es") is None


def test_host_check():
    assert check_host("localhost:8765", 8765)
    assert check_host("127.0.0.1:8765", 8765)
    assert not check_host("evil.example:8765", 8765)
    assert not check_host(None, 8765)


def test_example_config_loads():
    from pathlib import Path

    from jarvis.config import load_config
    cfg = load_config(Path(__file__).parent.parent / "jarvis" / "config.example.yaml")
    assert cfg.tools.search_paths == [Path.home()]
    assert cfg.llm.model.startswith("qwen")


def test_launch_reports_failure_and_success():
    from conftest import run

    from jarvis.tools import proc
    ok, err = run(proc.launch(["sh", "-c", "echo kaputt >&2; exit 3"]))
    assert not ok and "Exit-Code 3" in err and "kaputt" in err
    assert run(proc.launch(["sh", "-c", "exit 0"])) == (True, "")
    assert run(proc.launch(["sleep", "5"], wait=0.3)) == (True, "")
    ok, err = run(proc.launch(["/gibt/es/nicht"]))
    assert not ok


def test_desktop_env_from_systemd(monkeypatch):
    from jarvis.tools import proc
    for k in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(proc, "_systemd_user_env",
                        lambda: {"WAYLAND_DISPLAY": "wayland-0", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/x"})
    env = proc.desktop_env()
    assert env["WAYLAND_DISPLAY"] == "wayland-0" and env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/x"
    assert proc.has_display(env)


def test_open_file_resolves_name_and_reports_errors(cfg, tmp_path, monkeypatch):
    from conftest import run

    from jarvis.tools import files, proc
    from jarvis.tools.registry import ToolContext
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "Bericht.pdf").write_text("x")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "notiz.txt").write_text("1")
    (tmp_path / "b" / "notiz.txt").write_text("2")
    calls = []

    async def fake_launch(argv, wait=2.0):
        calls.append(argv)
        return (False, "Exit-Code 4") if "kaputt" in argv[-1] else (True, "")

    monkeypatch.setattr(proc, "launch", fake_launch)
    monkeypatch.setattr(files.shutil, "which", lambda n: None if n in ("plocate", "locate", "fd", "fdfind") else f"/usr/bin/{n}")
    ctx = ToolContext(cfg=cfg, memory=None)

    out = run(files.open_file(ctx, "bericht.pdf"))
    assert out == f"Geöffnet: {tmp_path / 'docs' / 'Bericht.pdf'}"
    assert calls[-1] == ["xdg-open", str(tmp_path / "docs" / "Bericht.pdf")]
    assert "Mehrere Dateien" in run(files.open_file(ctx, "notiz.txt"))
    assert "Nicht gefunden" in run(files.open_file(ctx, "fehlt.odt"))
    (tmp_path / "kaputt.txt").write_text("x")
    assert "fehlgeschlagen" in run(files.open_file(ctx, str(tmp_path / "kaputt.txt")))


def test_exec_argv_places_files():
    from jarvis.tools.apps import _exec_argv
    assert _exec_argv("kate -b %U", ["/a", "/b"]) == ["kate", "-b", "/a", "/b"]
    assert _exec_argv("gimp-2.10 %f", ["/x.png"]) == ["gimp-2.10", "/x.png"]
    assert _exec_argv("code --new-window", ["/y"]) == ["code", "--new-window", "/y"]
    assert _exec_argv("app --icon %i %%done") == ["app", "--icon", "%done"]
