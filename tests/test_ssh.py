"""SSH zu anderen Rechnern: Anmeldedialog (Benutzer + Passwort nur im Dashboard), Einstufung der Befehle, sudo auf dem
Zielrechner ohne das Passwort in die Eingabe des Befehls zu geben."""

import asyncio

from conftest import run

from orbwise import askpass
from orbwise.config import Config
from orbwise.tools import ssh
from orbwise.tools.registry import BLOCKED, CONFIRM, SAFE, ToolContext


def make_cfg(**hosts):
    return Config.model_validate({"ssh": {"hosts": hosts}})


def test_resolve_short_names_and_addresses():
    cfg = make_cfg(nas={"host": "192.168.1.10", "user": "admin"}, pi={"host": "pi.local", "port": 2200})
    assert ssh.resolve(cfg, "NAS") == ("nas", "192.168.1.10", 22, "admin")
    assert ssh.resolve(cfg, "192.168.1.10") == ("nas", "192.168.1.10", 22, "admin")  # Adresse eines Kurznamens
    assert ssh.resolve(cfg, "pi") == ("pi", "pi.local", 2200, "")
    assert ssh.resolve(cfg, "root@server.lan:2022") == ("server.lan", "server.lan", 2022, "root")
    assert ssh.resolve(cfg, "[fe80::1]:22") == ("fe80::1", "fe80::1", 22, "")
    assert ssh.resolve(cfg, "nas", user="ich") == ("nas", "192.168.1.10", 22, "ich")


def test_commands_are_classified_like_local_ones():
    ctx = ToolContext(cfg=make_cfg(), memory=None)
    assert ssh._risk(ctx, {"host": "nas", "command": "df -h"})[0] == SAFE
    assert ssh._risk(ctx, {"host": "nas", "command": "docker restart jellyfin"})[0] == CONFIRM
    assert ssh._risk(ctx, {"host": "nas", "command": "sudo apt upgrade -y"})[0] == CONFIRM
    assert ssh._risk(ctx, {"host": "nas", "command": "rm -rf /"})[0] == BLOCKED
    assert "nas" in ssh._risk(ctx, {"host": "nas", "command": "touch x"})[1]


def test_sudo_detection_and_wrapping():
    for cmd in ("sudo whoami", "cd /x && sudo ls", "ls | sudo tee y", "if true; then sudo id; fi"):
        assert ssh._SUDO.search(cmd), cmd
    for cmd in ("grep sudo /etc/group", "echo sudo", "visudo-check"):
        assert not ssh._SUDO.search(cmd), cmd
    wrapped = ssh.with_sudo("sudo tee /etc/x")
    # Passwort nur für sudo -v; der Befehl selbst bekommt eine leere Eingabe und läuft nicht als letzter exec
    assert wrapped.index("sudo -S -p '' -v") < wrapped.index("exec </dev/null") < wrapped.index("sudo tee")
    assert wrapped.endswith("\n}; exit $?")


def _broker(answer=None):
    events = []

    async def notify(ev):
        events.append(ev)
        if ev["type"] == "password_request" and answer:
            asyncio.get_running_loop().call_soon(lambda: broker.answer(ev["id"], *answer))
    broker = askpass.AskpassBroker(1, notify=notify)
    return broker, events


def test_login_dialog_returns_user_and_password():
    broker, events = _broker(("geheim", False, "admin"))
    got = run(broker.ask_login("SSH-Anmeldung bei nas", command="ssh nas", user="ich"))
    assert got == ("admin", "geheim", False)
    req = events[0]
    assert req["kind"] == "login" and req["ask_user"] and req["user"] == "ich" and "geheim" not in str(req)


def test_login_dialog_cancel_and_remote():
    broker, _ = _broker((None, False, ""))
    assert run(broker.ask_login("x")) is None
    broker, events = _broker(("pw", False, "u"))

    async def from_phone():
        askpass.REMOTE.set(True)
        return await broker.ask_login("x")
    assert run(from_phone()) is None and not events  # Telegram/Routinen: nie ein Dialog


def test_secret_token_hands_out_password_only_briefly(tmp_path):
    helper = tmp_path / "askpass"
    helper.write_text("#!/bin/sh\n")
    broker = askpass.AskpassBroker(1, helper=helper)
    with broker.grant_secret("geheim", uses=2) as env:
        assert env["SSH_ASKPASS_REQUIRE"] == "force" and "geheim" not in str(env)
        token = env["ORBWISE_ASKPASS_TOKEN"]
        assert run(broker.request(token)) == "geheim"
        assert run(broker.request(token)) == "geheim"
        assert run(broker.request(token)) is None  # danach nicht mehr
    assert run(broker.request(token)) is None
    assert run(broker.request("falsch")) is None


def test_run_asks_login_once_and_reuses_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    helper = tmp_path / "askpass"
    helper.write_text("#!/bin/sh\n")
    asked = []

    async def notify(ev):
        if ev["type"] == "password_request":
            asked.append(ev)
            asyncio.get_running_loop().call_soon(lambda: broker.answer(ev["id"], "geheim", False, "admin"))
    broker = askpass.AskpassBroker(1, notify=notify, helper=helper)
    monkeypatch.setattr(askpass, "BROKER", broker)
    monkeypatch.setattr(ssh, "SESSIONS", {})
    calls, alive = [], set()

    async def fake_call(argv, timeout, env=None, stdin_text=None):
        calls.append(argv)
        if "-M" in argv:
            assert env["SSH_ASKPASS_REQUIRE"] == "force"
            assert run_secret(env) == "geheim"
            alive.add(argv[argv.index("-S") + 1])
            return 0, ""
        if "check" in argv:
            return (0 if argv[argv.index("-S") + 1] in alive else 255), ""
        return 0, ""

    def run_secret(env):
        info = broker._check(env["ORBWISE_ASKPASS_TOKEN"])
        return info["secret"]

    async def fake_run(s, command, timeout, ctx=None, stdin_text=None):
        return 0, f"ran {command} as {s.user}"
    monkeypatch.setattr(ssh, "_call", fake_call)
    monkeypatch.setattr(ssh, "_run", fake_run)
    cfg = make_cfg(nas={"host": "10.0.0.2", "user": "ich"})
    ctx = ToolContext(cfg=cfg, memory=None)
    first = run(ssh.ssh_run(ctx, "nas", "df -h"))
    second = run(ssh.ssh_run(ctx, "nas", "uptime"))
    assert "ran df -h as admin" in first and "ran uptime as admin" in second
    assert len(asked) == 1 and asked[0]["user"] == "ich"  # nur einmal gefragt, Vorschlag aus der Config
    master = next(a for a in calls if "-M" in a)
    assert "PubkeyAuthentication=no" in master and "StrictHostKeyChecking=accept-new" in master
    assert "geheim" not in " ".join(" ".join(a) for a in calls)  # Passwort nie in der Befehlszeile
    assert "Getrennt" in run(ssh.ssh_disconnect(ctx, "nas")) and not ssh.SESSIONS


def test_wrong_password_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    helper = tmp_path / "askpass"
    helper.write_text("#!/bin/sh\n")

    async def notify(ev):
        if ev["type"] == "password_request":
            asyncio.get_running_loop().call_soon(lambda: broker.answer(ev["id"], "falsch", False, "admin"))
    broker = askpass.AskpassBroker(1, notify=notify, helper=helper)
    monkeypatch.setattr(askpass, "BROKER", broker)
    monkeypatch.setattr(ssh, "SESSIONS", {})

    async def fake_call(argv, timeout, env=None, stdin_text=None):
        return 255, "admin@10.0.0.2: Permission denied (publickey,password)."
    monkeypatch.setattr(ssh, "_call", fake_call)
    out = run(ssh.ssh_connect(ToolContext(cfg=make_cfg(), memory=None), "10.0.0.2"))
    assert "abgelehnt" in out and not ssh.SESSIONS


def test_no_dashboard_no_login(monkeypatch):
    monkeypatch.setattr(askpass, "BROKER", None)
    monkeypatch.setattr(ssh, "SESSIONS", {})
    out = run(ssh.ssh_run(ToolContext(cfg=make_cfg(), memory=None), "nas", "ls"))
    assert "Dashboard" in out
