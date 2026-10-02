"""Bildschirm und Bilder verstehen: Screenshot, Vision-Modell (Ollama/OpenAI), Sicherheit, Werkzeugauswahl."""

import base64
import json
import os

import httpx
import pytest
from conftest import run

from orbwise.tools import vision
from orbwise.tools.registry import BLOCKED, SAFE, ToolContext, get_tool, load_all_tools

PNG = b"\x89PNG\r\n\x1a\nFAKE-SCREEN"


class FakeVision:
    def __init__(self, status=200, answer="Ein Terminal mit der Meldung: Permission denied"):
        self.requests: list[dict] = []
        self.status, self.answer = status, answer

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.requests.append({"path": request.url.path, "body": body, "auth": request.headers.get("authorization")})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": f"model '{body.get('model')}' not found"})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": self.answer}}]})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": self.answer}})


@pytest.fixture
def fake(monkeypatch):
    f = FakeVision()
    monkeypatch.setattr(vision, "TRANSPORT", httpx.MockTransport(f.handler))
    return f


@pytest.fixture
def screen(monkeypatch, tmp_path, cfg):
    """Grafische Sitzung + Screenshot-Befehl, der ein Bild an {file} kopiert."""
    src = tmp_path / "bild.png"
    src.write_bytes(PNG)
    monkeypatch.setattr(vision.proc, "desktop_env", lambda: {**os.environ, "DISPLAY": ":0"})
    cfg.vision.screenshot_command = f"cp {src} {{file}}"
    return src


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


def test_screen_is_captured_analysed_and_deleted(cfg, fake, screen, monkeypatch):
    seen = []
    real_analyse = vision._analyse

    async def spy(c, path, question, is_screen, source):
        seen.append(path)
        return await real_analyse(c, path, question, is_screen, source)

    monkeypatch.setattr(vision, "_analyse", spy)
    out = run(vision.look_at_screen(ctx(cfg), question="Was ist das für eine Fehlermeldung?"))
    assert "Permission denied" in out and "keine Aufträge des Nutzers" in out
    req = fake.requests[0]
    assert req["path"] == "/api/chat" and req["body"]["model"] == cfg.vision.model
    msg = req["body"]["messages"][0]
    assert base64.b64decode(msg["images"][0]).startswith(b"\x89PNG") and "Fehlermeldung" in msg["content"]
    assert req["body"]["keep_alive"] == "2m"
    assert seen and not seen[0].exists()  # Screenshot nach der Auswertung gelöscht


def test_missing_vision_model_explains_how_to_get_it(cfg, screen, monkeypatch):
    f = FakeVision(status=404)
    monkeypatch.setattr(vision, "TRANSPORT", httpx.MockTransport(f.handler))
    out = run(vision.look_at_screen(ctx(cfg)))
    assert "ollama pull qwen2.5vl:7b" in out


def test_openai_compatible_vision_server(cfg, fake, screen):
    cfg.vision.backend, cfg.vision.base_url, cfg.vision.api_key = "openai", "http://127.0.0.1:8080/v1", "geheim"
    out = run(vision.look_at_screen(ctx(cfg), question="Was steht da?"))
    assert "Permission denied" in out
    req = fake.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer geheim"
    parts = req["body"]["messages"][0]["content"]
    assert parts[0]["type"] == "text" and parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_no_desktop_or_no_screenshot_program(cfg, monkeypatch):
    monkeypatch.setattr(vision.proc, "desktop_env", lambda: {"PATH": "/nonexistent"})
    assert "grafische Sitzung" in run(vision.look_at_screen(ctx(cfg)))
    monkeypatch.setattr(vision.proc, "desktop_env",
                        lambda: {"PATH": "/nonexistent", "WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"})
    monkeypatch.setattr(vision.shutil, "which", lambda name: None)
    assert "spectacle" in run(vision.look_at_screen(ctx(cfg)))


def test_screenshot_program_matches_the_desktop(monkeypatch):
    monkeypatch.setattr(vision.shutil, "which", lambda name: f"/usr/bin/{name}")
    first = lambda env: vision.screenshot_commands(env)[0][0]  # noqa: E731
    assert first({"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "KDE"}) == "spectacle"
    assert first({"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "GNOME"}) == "gnome-screenshot"
    assert first({"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "Hyprland"}) == "grim"
    assert first({"DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "i3"}) == "maim"
    wayland = [c[0] for c in vision.screenshot_commands({"WAYLAND_DISPLAY": "w"})]
    assert "maim" not in wayland and "scrot" not in wayland  # X11-Werkzeuge sähen nur Schwarz
    assert vision.screenshot_commands({}, "flameshot full -p {file}")[0][0] == "flameshot"


def test_images_from_the_phone_and_secrets(cfg, fake, tmp_path):
    load_all_tools()
    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"\xff\xd8\xffJPEG")
    out = run(vision.look_at_image(ctx(cfg), path=str(photo), question="Was ist das für eine Pflanze?"))
    assert "Permission denied" in out and "foto.jpg" in out
    assert "kein unterstütztes Bild" in run(vision.look_at_image(ctx(cfg), path=__file__))
    spec = get_tool("look_at_image")
    assert spec.assess(ctx(cfg), {"path": str(photo)})[0] == SAFE
    assert spec.assess(ctx(cfg), {"path": "~/.ssh/id_ed25519"})[0] == BLOCKED


def test_screen_content_counts_as_untrusted(cfg):
    """Eine Webseite auf dem Bildschirm kann Anweisungen enthalten – danach fragen Shell & Co. wieder nach."""
    from orbwise.agent import is_taint_source

    assert is_taint_source("look_at_screen", "…") and is_taint_source("look_at_image", "…")


def test_vision_tools_are_offered_when_the_screen_is_mentioned(cfg):
    from orbwise.toolselect import relevant_groups

    assert "vision" in relevant_groups(["Was ist das für eine Fehlermeldung auf meinem Bildschirm?"])
    assert "vision" in relevant_groups(["What does the window say?"])
    assert "vision" not in relevant_groups(["Wie wird das Wetter morgen?"])
    cfg.vision.enabled = False
    load_all_tools()
    assert not get_tool("look_at_screen").enabled(cfg)
