"""Routinen: Zeitplan, Speicher, Tools, Ausführung im eigenen Chat, Bestätigungen, API."""

import time
from datetime import datetime, timedelta

import pytest
from conftest import run
from fastapi.testclient import TestClient

from orbwise import prompts
from orbwise.routines import Routine, RoutineStore, days_label, parse_days, parse_time
from orbwise.tools import routine_tools
from orbwise.tools.registry import CONFIRM, SAFE, ToolContext, get_tool, load_all_tools

MON_7 = datetime(2026, 9, 28, 7, 0)  # Montag


def store(tmp_path) -> RoutineStore:
    return RoutineStore(tmp_path / "routines.json")


# ---------------------------------------------------------------- Zeitplan

def test_parsers():
    assert parse_time("8") == "08:00" and parse_time("8.30 Uhr") == "08:30" and parse_time("23:59") == "23:59"
    for bad in ("25:00", "8:61", "morgens"):
        with pytest.raises(ValueError):
            parse_time(bad)
    assert parse_days("werktags") == [0, 1, 2, 3, 4] == parse_days("Mo bis Fr")
    assert parse_days("Wochenende") == [5, 6] and parse_days("täglich") == [] and parse_days("") == []
    assert parse_days("montags und mittwochs") == [0, 2] and parse_days("Fr-Mo") == [0, 4, 5, 6]
    assert parse_days([4, "sa"]) == [4, 5]
    with pytest.raises(ValueError):
        parse_days("irgendwann")
    assert days_label([]) == "täglich" and days_label([0, 1, 2, 3, 4]) == "Mo–Fr" and days_label([0, 2, 4]) == "Mo, Mi, Fr"
    assert days_label([5, 6], en=True) == "weekends"


def test_next_and_previous_run():
    r = Routine(id="a", name="News", task="x", time="08:00", days=[0, 1, 2, 3, 4])
    assert r.next_run(MON_7) == datetime(2026, 9, 28, 8, 0)
    assert r.next_run(datetime(2026, 10, 2, 9, 0)) == datetime(2026, 10, 5, 8, 0)  # Fr nach 8 → Mo
    assert r.previous_run(datetime(2026, 10, 4, 12, 0)) == datetime(2026, 10, 2, 8, 0)  # So → Fr
    daily = Routine(id="b", name="Abend", task="x", time="23:30")
    assert daily.next_run(datetime(2026, 9, 28, 23, 45)) == datetime(2026, 9, 29, 23, 30)
    once = Routine(id="c", name="Einmal", task="x", time="09:00", date="2026-10-01")
    assert once.next_run(MON_7) == datetime(2026, 10, 1, 9, 0) and once.next_run(datetime(2026, 10, 2)) is None
    assert Routine(id="d", name="Aus", task="x", time="08:00", enabled=False).next_run(MON_7) is None
    assert once.schedule() == "einmalig 01.10.2026 09:00" and r.schedule() == "Mo–Fr 08:00"


def test_due_catch_up_and_no_double_run(tmp_path):
    s = store(tmp_path)
    r = s.add("News", "Suche Linux-News", "08:00", "werktags", now=datetime(2026, 9, 27, 12, 0))
    assert s.due(MON_7) == []  # noch nicht so weit
    at = datetime(2026, 9, 28, 8, 0, 30)
    assert [x.id for x in s.due(at)] == [r.id]
    s.mark_started(r.id, at)
    assert s.due(at + timedelta(seconds=20)) == []  # läuft bzw. schon gelaufen
    s.set_result(r.id, "ok", "fertig", "chat1")
    assert s.due(at + timedelta(minutes=5)) == []
    assert [x.id for x in s.due(datetime(2026, 9, 29, 8, 30))] == [r.id]  # nächster Tag, 30 min verspätet
    assert s.due(datetime(2026, 9, 29, 10, 0)) == []  # zu spät (PC war aus) → nicht nachholen
    new = s.add("Neu", "x", "07:00", now=datetime(2026, 9, 28, 7, 30))
    assert new.id not in [x.id for x in s.due(datetime(2026, 9, 28, 7, 40))]  # vor dem Anlegen fällig → nicht


def test_once_routine_disables_itself_and_store_roundtrip(tmp_path):
    s = store(tmp_path)
    r = s.add("", "Einmal prüfen", "09:00", day="2099-01-01")
    assert r.name == "Einmal prüfen" and r.date == "2099-01-01" and r.days == []
    s.set_result(r.id, "ok", "erledigt", "c1")
    again = store(tmp_path)
    assert not again.get(r.id).enabled and again.get(r.id).chat_id == "c1" and again.get(r.id).last_summary == "erledigt"
    with pytest.raises(ValueError, match="Vergangenheit"):
        s.add("alt", "x", "09:00", day="2000-01-01")
    with pytest.raises(ValueError, match="was die Routine tun soll"):
        s.add("leer", " ", "09:00")
    s.update(r.id, time="10:15", days="Wochenende", date="", enabled=True)
    assert again.get(r.id) is not None and store(tmp_path).get(r.id).days == [5, 6]
    s.mark_started(r.id, datetime.now())
    s.reset_running()
    assert s.get(r.id).last_status == "error"
    assert s.delete(r.id) and not s.delete(r.id)


# ---------------------------------------------------------------- Tools

def ctx(cfg, services=None):
    return ToolContext(cfg=cfg, memory=None, services=services if services is not None else {})


def test_tools_create_list_update_delete(cfg):
    load_all_tools()
    c = ctx(cfg)
    risk, reason = get_tool("routine_create").assess(c, {"task": "Suche Linux-News", "time": "8", "days": "werktags",
                                                        "name": "Linux-News"})
    assert risk == CONFIRM and "„Linux-News“ – Mo–Fr um 08:00" in reason and "Suche Linux-News" in reason
    out = run(routine_tools.routine_create(c, "Suche Linux-News", "8", "Linux-News", "werktags"))
    assert out.startswith("✔ Routine angelegt: „Linux-News“ – Mo–Fr 08:00, nächste Ausführung")
    assert "nicht angelegt" in run(routine_tools.routine_create(c, "x", "25:00"))
    assert "nicht angelegt" in run(routine_tools.routine_create(c, "x", "08:00", days="blub"))
    assert "Aufgabe: Suche Linux-News" in run(routine_tools.routine_list(c))
    assert get_tool("routine_list").risk == SAFE and get_tool("routine_run_now").risk == SAFE
    risk, reason = get_tool("routine_update").assess(c, {"which": "linux", "time": "09:00"})
    assert risk == CONFIRM and "„Linux-News“ (Mo–Fr 08:00) ändern: time → 09:00" in reason
    out = run(routine_tools.routine_update(c, "linux", time="9", enabled="nein"))
    assert "Mo–Fr 09:00 (pausiert)" in out
    assert "Keine Routine" in run(routine_tools.routine_delete(c, "gibtsnicht"))
    assert "gelöscht" in run(routine_tools.routine_delete(c, "Linux-News"))
    assert "keine Routinen" in run(routine_tools.routine_list(c))


def test_run_now_uses_server_hook(cfg):
    started = []
    c = ctx(cfg, {"start_routine": lambda rid: started.append(rid) or True})
    run(routine_tools.routine_create(c, "Wetter prüfen", "07:00", "Wetter"))
    assert "startet gleich" in run(routine_tools.routine_run_now(c, "wetter")) and len(started) == 1
    assert "nur laufen, während" in run(routine_tools.routine_run_now(ctx(cfg), "wetter"))


# ---------------------------------------------------------------- Ausführung (Server, FakeLLM)

@pytest.fixture
def app(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    return create_app(cfg)


def wait_done(client, rid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        r = next(x for x in client.get("/api/routines").json() if x["id"] == rid)
        if r["last_status"] not in ("", "running"):
            return r
        time.sleep(0.05)
    raise AssertionError("Routine wurde nicht fertig")


def test_routine_runs_in_its_own_chat(app):
    memory = app.state.memory
    with TestClient(app, base_url="http://localhost:8765") as client:
        memory.conversation.add({"role": "user", "content": "Mein eigener Chat"})
        mine = memory.conversation.chat_id
        rid = client.post("/api/routines", json={"name": "Linux-News", "task": "Suche die neuesten Linux-News",
                                                 "time": "08:00", "days": [0, 1, 2, 3, 4]}).json()["id"]
        assert client.post(f"/api/routines/{rid}/run").json() == {"ok": True}
        r = wait_done(client, rid)
        assert r["last_status"] == "ok" and "Linux-News" in r["last_summary"] and r["chat_id"] != mine
        assert memory.conversation.chat_id == mine and memory.chats.active_id() == mine
        assert [m["content"] for m in memory.conversation.history] == ["Mein eigener Chat"]
        chats = {c["id"]: c for c in client.get("/api/chats").json()}
        assert chats[r["chat_id"]]["title"] == "⟳ Linux-News" and not chats[r["chat_id"]]["active"]
        client.post(f"/api/routines/{rid}/run")
        r2 = wait_done(client, rid)
        assert r2["chat_id"] == r["chat_id"]  # zweiter Lauf im selben Chat
        assert chats[r["chat_id"]]["messages"] >= 2


def test_confirmation_without_ui_is_denied(app, monkeypatch):
    monkeypatch.setitem(prompts.TEXTS["de"], "routine_prompt", "{task}")
    with TestClient(app, base_url="http://localhost:8765") as client:
        rid = client.post("/api/routines", json={"name": "Aufräumen", "time": "08:00",
                                                 "task": '/tool run_shell {"command": "rm -rf /tmp/orbwise-test-x"}'}).json()["id"]
        client.post(f"/api/routines/{rid}/run")
        assert wait_done(client, rid)["last_status"] == "denied"


def test_routine_api(app):
    with TestClient(app, base_url="http://localhost:8765") as client:
        r = client.post("/api/routines", json={"task": "Wetter", "time": "7", "days": [5, 6]}).json()
        assert r["schedule"] == "am Wochenende 07:00" and r["next"] != "–" and r["enabled"]
        assert client.post("/api/routines", json={"task": "x", "time": "99"}).status_code == 400
        assert client.post("/api/routines", json=[1]).status_code == 400
        u = client.put(f"/api/routines/{r['id']}", json={"enabled": False, "time": "06:30"}).json()
        assert not u["enabled"] and u["time"] == "06:30" and u["next"] == "–"
        assert client.put("/api/routines/nix", json={}).status_code == 404
        assert client.post("/api/routines", json={"task": "x", "time": "8"},
                           headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.delete(f"/api/routines/{r['id']}").json() == {"ok": True}
        assert client.delete(f"/api/routines/{r['id']}").status_code == 404
        assert client.post("/api/routines/nix/run").status_code == 404
