import pytest
from fastapi.testclient import TestClient

from orbwise import prompts
from orbwise.agent import Agent
from orbwise.config import Config
from orbwise.server import CALL_TEXTS, describe_call, parse_yes_no
from orbwise.web_i18n import HTML_EN, translate_index


def test_language_defaults():
    de = Config()
    assert de.language == "de" and de.voice.language == "de" and de.voice.tts_voice.name == "de_DE-thorsten-high.onnx"
    en = Config(language="EN")
    assert en.language == "en" and en.voice.language == "en" and en.voice.tts_voice.name == "en_GB-alan-medium.onnx"
    assert Config(language="fr").language == "de"
    assert Config(language="en", voice={"language": "de"}).voice.language == "de"  # explizit gesetzt bleibt


def test_english_prompt_and_texts(cfg, llm, memory):
    cfg.language = "en"
    cfg.calendar.url = cfg.calendar.username = cfg.calendar.password = "x"
    p = Agent(cfg, llm, memory).system_prompt()
    assert "Always answer in English" in p and "calendar_events" in p and "Antworte" not in p
    assert prompts.spoken(cfg, "confirm", what=describe_call("install_package", {"names": "htop"}, cfg)) == \
        "Shall I run the installation of htop?"
    cfg.language = "de"
    assert describe_call("system_update", {}, cfg) == "ein vollständiges Systemupdate"
    assert set(CALL_TEXTS) <= {"run_shell", "install_package", "remove_package", "system_update", "calendar_update",
                               "calendar_delete", "trilium_update_note", "write_file"}


@pytest.mark.parametrize("text,expected", [("yes please", True), ("go ahead", True), ("nope", False),
                                           ("cancel that", False), ("ja", True), ("nein danke", False),
                                           ("what?", None)])
def test_yes_no_both_languages(text, expected):
    assert parse_yes_no(text) is expected


def test_index_translation_complete():
    from pathlib import Path
    html = (Path(__file__).parent.parent / "orbwise" / "web" / "index.html").read_text(encoding="utf-8")
    assert all(de in html for de, _ in HTML_EN), "index.html geändert – HTML_EN anpassen"
    en = translate_index(html, "en")
    assert '<html lang="en">' in en and "START SYSTEM" in en and "SYSTEM STARTEN" not in en
    assert translate_index(html, "de") == html


def test_served_page_uses_language(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    cfg.language = "en"
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        r = client.get("/")
        assert '<html lang="en">' in r.text and "COMMUNICATION" in r.text
