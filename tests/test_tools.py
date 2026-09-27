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
