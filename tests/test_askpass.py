import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import run
from fastapi.testclient import TestClient

from jarvis import askpass
from jarvis.tools import power as pw
from jarvis.tools import proc
from jarvis.tools.packages import helper_cmd, privileged
from jarvis.tools.registry import CONFIRM, SAFE, ToolContext
from jarvis.tools.safety import apply_privilege


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


# ---------------------------------------------------------------- Umschreiben der Befehle

@pytest.mark.parametrize("cmd,expected", [
    ("sudo shutdown now", "sudo -A shutdown now"),
    ("sudo -u root ls /root", "sudo -A -u root ls /root"),
    ("ls; sudo -n pacman -Syu", "ls; sudo -A pacman -Syu"),
    ("echo x | sudo tee /etc/f", "echo x | sudo -A tee /etc/f"),
    ("pkexec systemctl restart sshd", "sudo -A systemctl restart sshd"),
    ("ls -la", "ls -la"),
])
def test_apply_privilege_jarvis(cmd, expected):
    assert apply_privilege(cmd, "jarvis") == expected


def test_default_mode_and_package_helpers(cfg):
    assert cfg.tools.privilege_cmd == "jarvis"
    assert privileged(ctx(cfg), ["pacman", "-Syu"]) == ["sudo", "-A", "pacman", "-Syu"]
    assert helper_cmd(ctx(cfg), "yay", "-Sua")[1:3] == ["--sudoflags", "-A"]
    cfg.tools.privilege_cmd = "pkexec"
    assert privileged(ctx(cfg), ["pacman", "-Syu"])[0] == "pkexec"


# ---------------------------------------------------------------- Umgebung & Token-Lebensdauer

def test_proc_run_grants_token_only_for_sudo_a(cfg, tmp_path, monkeypatch):
    helper = askpass.write_helper(tmp_path / "rt" / "jarvis" / "askpass")
    assert oct(helper.stat().st_mode)[-3:] == "700"
    broker = askpass.AskpassBroker(8765, helper=helper)
    monkeypatch.setattr(askpass, "BROKER", broker)
    rc, out = run(proc.run(ctx(cfg), ': sudo -A ; echo "$SUDO_ASKPASS|${JARVIS_ASKPASS_TOKEN:+token}"',
                           timeout=10, stream=False))
    assert out.strip() == f"{helper}|token"
    assert broker.tokens == {}  # nach dem Befehl verfallen
    rc, out = run(proc.run(ctx(cfg), 'echo "${JARVIS_ASKPASS_TOKEN:-keins}"', timeout=10, stream=False))
    assert out.strip() == "keins"


def test_root_denied_hint():
    out = proc.format_result(1, "sudo: no password was provided\nsudo: a password is required", 1000)
    assert "NICHT erteilt" in out and "Nicht mit anderen Befehlen" in out
    assert "NICHT erteilt" not in proc.format_result(0, "ok", 1000)
    assert "NICHT erteilt" not in proc.format_result(1, "No such file", 1000)


# ---------------------------------------------------------------- Helfer-Skript (so wie sudo es aufruft)

def test_helper_script_prints_password_and_ignores_proxy(tmp_path):
    seen = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen["token"] = self.headers.get("X-Jarvis-Askpass")
            seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"password": "geh eim!"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    helper = askpass.write_helper(tmp_path / "askpass")
    env = {**os.environ, "JARVIS_ASKPASS_URL": f"http://127.0.0.1:{srv.server_port}/api/askpass",
           "JARVIS_ASKPASS_TOKEN": "tok123", "http_proxy": "http://proxy.invalid:1", "HTTP_PROXY": "http://proxy.invalid:1"}
    res = subprocess.run([str(helper), "[sudo] Passwort für joshua: "], env=env, capture_output=True, text=True,
                         timeout=20)
    assert res.returncode == 0 and res.stdout == "geh eim!\n"
    assert seen == {"token": "tok123", "body": {"prompt": "[sudo] Passwort für joshua: "}}
    # ohne Token: sofort Fehler (sudo bricht dann ab)
    env.pop("JARVIS_ASKPASS_TOKEN")
    assert subprocess.run([str(helper)], env=env, capture_output=True, timeout=20).returncode == 1


# ---------------------------------------------------------------- Server: Passwortfeld in der Oberfläche

@pytest.fixture
def app_client(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_FAKE_LLM", "1")
    monkeypatch.setenv("JARVIS_SKIP_WARMUP", "1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    from jarvis.server import create_app
    return TestClient(create_app(cfg), base_url="http://localhost:8765")


def _next(ws, kind):
    for _ in range(50):
        ev = ws.receive_json()
        if ev["type"] == kind:
            return ev
    raise AssertionError(kind)


def test_password_roundtrip_via_ui(app_client, caplog, tmp_path):
    with app_client as client:
        broker = askpass.BROKER
        assert broker.ready() and broker.helper == tmp_path / "run" / "jarvis" / "askpass"
        # falsches Token → abgelehnt, ohne die Oberfläche zu fragen
        r = client.post("/api/askpass", json={"prompt": "x"}, headers={"X-Jarvis-Askpass": "falsch"})
        assert r.status_code == 403
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()  # hello
            with broker.grant("sudo -A systemctl poweroff") as env:
                token = env["JARVIS_ASKPASS_TOKEN"]
                result = {}

                def helper_call():
                    result["r"] = client.post("/api/askpass", json={"prompt": "[sudo] Passwort"},
                                              headers={"X-Jarvis-Askpass": token})

                t = threading.Thread(target=helper_call)
                t.start()
                ev = _next(ws, "password_request")
                assert ev["command"] == "sudo -A systemctl poweroff" and not ev["retry"]
                ws.send_json({"type": "password", "id": ev["id"], "password": "Sehr-Geheim-42"})
                t.join(10)
                assert result["r"].status_code == 200 and result["r"].json() == {"password": "Sehr-Geheim-42"}
                assert result["r"].headers["cache-control"] == "no-store"
                assert _next(ws, "password_done")["id"] == ev["id"]

                # zweiter Versuch (falsches Passwort) → Abbruch in der Oberfläche
                t = threading.Thread(target=helper_call)
                t.start()
                ev = _next(ws, "password_request")
                assert ev["retry"]
                ws.send_json({"type": "password_cancel", "id": ev["id"]})
                t.join(10)
                assert result["r"].status_code == 403
            # nach Befehlsende ist das Token ungültig
            r = client.post("/api/askpass", json={}, headers={"X-Jarvis-Askpass": token})
            assert r.status_code == 403
    assert "Sehr-Geheim-42" not in caplog.text
    hist = json.dumps(client.app.state.memory.conversation.history)
    assert "Sehr-Geheim-42" not in hist


def test_no_ui_open_means_denied(app_client):
    with app_client as client:
        with askpass.BROKER.grant("sudo -A ls") as env:
            r = client.post("/api/askpass", json={}, headers={"X-Jarvis-Askpass": env["JARVIS_ASKPASS_TOKEN"]})
            assert r.status_code == 403


# ---------------------------------------------------------------- Power-Tool

class Calls(list):
    replies: list


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()
    replies = []

    async def fake_run(ctx, cmd, timeout, stream=True, cwd=None):
        seen.append(cmd)
        return replies.pop(0) if replies else (0, "")

    monkeypatch.setattr(pw.proc, "run", fake_run)
    monkeypatch.setattr(pw, "START_DELAY", 0)
    seen.replies = replies
    return seen


def test_power_risks(cfg):
    assert pw._risk(ctx(cfg), {"action": "poweroff"})[0] == CONFIRM
    assert pw._risk(ctx(cfg), {"action": "Neustart"})[0] == CONFIRM
    assert pw._risk(ctx(cfg), {"action": "lock"})[0] == SAFE
    assert "in 10 Minuten" in pw._risk(ctx(cfg), {"action": "shutdown", "delay_minutes": 10})[1]


def test_power_poweroff_runs_without_root_after_delay(cfg, calls):
    import asyncio

    async def go():
        out = await pw.power(ctx(cfg), "shutdown")
        await asyncio.sleep(0.05)
        return out

    out = run(go())
    assert "heruntergefahren" in out
    assert calls == [["systemctl", "poweroff"]]


def test_power_fallback_to_password_and_delay(cfg, calls):
    calls.replies.extend([(1, "Access denied"), (0, "")])
    out = run(pw.power(ctx(cfg), "poweroff", delay_minutes=10))
    assert calls == [["shutdown", "-h", "+10"], ["sudo", "-A", "shutdown", "-h", "+10"]]
    assert "in 10 Minuten heruntergefahren" in out
    calls.clear()
    assert run(pw.power(ctx(cfg), "lock")) == "Erledigt: den Bildschirm sperren."
    assert calls == [["loginctl", "lock-session"]]
    assert "Unbekannte Aktion" in run(pw.power(ctx(cfg), "explodieren"))
