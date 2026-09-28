from datetime import datetime, timedelta

import httpx
import pytest
from conftest import run
from fastapi.testclient import TestClient

from jarvis.reminders import ReminderStore, parse_when
from jarvis.tools import briefing, proc, reminder_tools, weather, web
from jarvis.tools.registry import ToolContext


def ctx(cfg):
    return ToolContext(cfg=cfg, memory=None)


# ---------------------------------------------------------------- Websites

def test_website_urls():
    assert web.website_url("YouTube") == "https://www.youtube.com"
    assert web.website_url("youtube", "lofi hip hop") == "https://www.youtube.com/results?search_query=lofi+hip+hop"
    assert web.website_url("amazon", "Lüfter 120mm") == "https://www.amazon.de/s?k=L%C3%BCfter+120mm"
    assert web.website_url("wiki", "Iron Man").startswith("https://de.wikipedia.org/w/index.php?search=Iron+Man")
    assert web.website_url("heise.de") == "https://heise.de"
    assert web.website_url("https://example.org/x") == "https://example.org/x"
    assert web.website_url("Proxmox Forum").startswith("https://duckduckgo.com/?q=%21ducky+Proxmox+Forum")
    extra = {"nas": "http://192.168.1.10:5000", "geizhals": "https://geizhals.de/?fs={q}"}
    assert web.website_url("NAS", extra=extra) == "http://192.168.1.10:5000"
    assert web.website_url("geizhals", "rtx 4080", extra) == "https://geizhals.de/?fs=rtx+4080"
    assert web.website_url("geizhals", "", extra) == "https://geizhals.de"


def test_open_website_uses_launch(cfg, monkeypatch):
    calls = []

    async def fake_launch(argv, wait=2.0):
        calls.append(argv)
        return True, ""

    monkeypatch.setattr(proc, "launch", fake_launch)
    monkeypatch.setattr(web.shutil, "which", lambda n: f"/usr/bin/{n}")
    out = run(web.open_website(ctx(cfg), "youtube", "jarvis"))
    assert out == "Geöffnet: https://www.youtube.com/results?search_query=jarvis"
    assert calls == [["xdg-open", "https://www.youtube.com/results?search_query=jarvis"]]


# ---------------------------------------------------------------- Wetter

FORECAST = {
    "current": {"temperature_2m": 14.3, "apparent_temperature": 12.8, "weather_code": 2,
                "wind_speed_10m": 11.2, "relative_humidity_2m": 71},
    "daily": {"time": ["2026-09-27", "2026-09-28", "2026-09-29"],
              "weather_code": [2, 61, 0], "temperature_2m_max": [18.4, 15.1, 20.0],
              "temperature_2m_min": [8.2, 9.0, 7.5], "precipitation_probability_max": [10, 80, 0],
              "precipitation_sum": [0, 6.4, 0], "sunrise": ["2026-09-27T07:21", None, None],
              "sunset": ["2026-09-27T19:12", None, None]},
}


@pytest.fixture
def fake_weather(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        if "geocoding" in req.url.host:
            name = req.url.params["name"]
            if name == "Nirgendwo":
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"results": [{"name": "Freiburg im Breisgau", "admin1": "Baden-Württemberg",
                                                          "latitude": 47.99, "longitude": 7.84}]})
        return httpx.Response(200, json=FORECAST)

    monkeypatch.setattr(weather, "TRANSPORT", httpx.MockTransport(handler))
    weather._geo_cache.clear()
    return seen


def test_weather_report(cfg, fake_weather):
    out = run(weather.weather(ctx(cfg), "Freiburg"))
    assert "Freiburg im Breisgau (Baden-Württemberg)" in out
    assert "Jetzt 14 °C (gefühlt 13 °C), teils bewölkt" in out
    assert "Heute: 8 bis 18 °C, teils bewölkt, Regenrisiko 10 %, Sonne 07:21–19:12." in out
    assert "Morgen: 9 bis 15 °C, leichter Regen, Regenrisiko 80 % (6,4 mm)." in out
    forecast_req = fake_weather[-1]
    assert forecast_req.url.params["latitude"] == "47.99" and forecast_req.url.params["timezone"] == "auto"


def test_weather_default_location_and_errors(cfg, fake_weather):
    assert "kein Standardort" in run(weather.weather(ctx(cfg)))
    cfg.weather.location = "Freiburg"
    assert "Freiburg im Breisgau" in run(weather.weather(ctx(cfg)))
    assert "nicht gefunden" in run(weather.weather(ctx(cfg), "Nirgendwo"))
    assert weather.describe(95) == "Gewitter" and weather.describe(None) == "unbekannt"


# ---------------------------------------------------------------- Erinnerungen

def test_parse_when():
    now = datetime(2026, 9, 27, 14, 0)
    assert parse_when(20, now=now) == datetime(2026, 9, 27, 14, 20)
    assert parse_when(at="15:30", now=now) == datetime(2026, 9, 27, 15, 30)
    assert parse_when(at="9:15", now=now) == datetime(2026, 9, 28, 9, 15)       # schon vorbei → morgen
    assert parse_when(at="2026-10-01 08:00", now=now) == datetime(2026, 10, 1, 8, 0)
    assert parse_when(at="2026-10-01T08:00", now=now) == datetime(2026, 10, 1, 8, 0)
    assert parse_when(at="03.10.2026 18:00", now=now) == datetime(2026, 10, 3, 18, 0)
    assert parse_when(at="24.12. 18:00", now=now) == datetime(2026, 12, 24, 18, 0)
    assert parse_when(at="15.30 Uhr", now=now) == datetime(2026, 9, 27, 15, 30)
    with pytest.raises(ValueError):
        parse_when(at="irgendwann", now=now)
    with pytest.raises(ValueError):
        parse_when()


def test_store_persistence_and_cancel(tmp_path):
    path = tmp_path / "reminders.json"
    store = ReminderStore(path)
    a = store.add("Pizza", datetime.now() + timedelta(minutes=5), "timer")
    store.add("Zahnarzt anrufen", datetime.now() + timedelta(hours=2))
    again = ReminderStore(path)
    assert [r.text for r in again.upcoming()] == ["Pizza", "Zahnarzt anrufen"]
    assert again.cancel("zahnarzt")[0].text == "Zahnarzt anrufen"
    assert again.cancel(a.id)[0].text == "Pizza"
    assert again.upcoming() == []


def test_due_and_done(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    r = store.add("Jetzt", datetime.now() - timedelta(seconds=1))
    store.add("Später", datetime.now() + timedelta(hours=1))
    assert [x.id for x in store.due()] == [r.id]
    store.mark_done(r.id)
    assert store.due() == [] and len(store.upcoming()) == 1


def test_reminder_tools(cfg):
    c = ctx(cfg)
    out = run(reminder_tools.set_reminder(c, "Pizza aus dem Ofen", in_minutes=12, timer=True))
    assert out.startswith("Gespeichert: Timer heute") or out.startswith("Gespeichert: Timer morgen")
    assert "Pizza aus dem Ofen" in run(reminder_tools.list_reminders(c))
    assert "Vergangenheit" in run(reminder_tools.set_reminder(c, "x", at="2020-01-01 10:00"))
    assert "nicht verstanden" in run(reminder_tools.set_reminder(c, "x", at="bald"))
    assert "Gelöscht" in run(reminder_tools.cancel_reminder(c, "pizza"))
    assert "keine anstehenden" in run(reminder_tools.list_reminders(c)).lower()
    assert (cfg.memory.dir.parent / "reminders.json").exists()


@pytest.fixture
def app_client(cfg, monkeypatch):
    monkeypatch.setenv("JARVIS_FAKE_LLM", "1")
    monkeypatch.setenv("JARVIS_SKIP_WARMUP", "1")
    from jarvis.server import create_app
    return TestClient(create_app(cfg), base_url="http://localhost:8765")


def test_scheduler_fires_reminder(cfg, app_client):
    store = ReminderStore(cfg.memory.dir.parent / "reminders.json")
    with app_client as client:
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()  # hello
            client.app.state.hub.agent.services["reminders"].add("Wäsche aufhängen", datetime.now())
            for _ in range(30):
                ev = ws.receive_json()
                if ev["type"] == "reminder":
                    break
            assert ev["type"] == "reminder" and ev["text"] == "Wäsche aufhängen" and not ev["late"]
            assert ev["spoken"] == "Erinnerung: Wäsche aufhängen"
    store.load()
    assert store.upcoming() == []
    journal = next((cfg.memory.dir / "journal").glob("*.md")).read_text()
    assert "Erinnerung: Wäsche aufhängen" in journal


def test_missed_reminder_after_restart(cfg, app_client):
    import time
    with app_client as client:
        # fällig geworden, während keine Oberfläche offen war
        client.app.state.hub.agent.services["reminders"].add("Alt", datetime.now() - timedelta(hours=1))
        time.sleep(1.5)
        assert client.app.state.hub.undelivered
        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.receive_json()
            for _ in range(30):
                ev = ws.receive_json()
                if ev["type"] == "reminder":
                    break
            assert ev["late"] and ev["spoken"].startswith("Verpasste Erinnerung von")


def test_reminder_api(cfg, app_client):
    with app_client as client:
        store = client.app.state.hub.agent.services["reminders"]
        r = store.add("API-Test", datetime.now() + timedelta(hours=3))
        assert client.get("/api/reminders").json()[0]["text"] == "API-Test"
        assert client.delete(f"/api/reminders/{r.id}", headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.delete(f"/api/reminders/{r.id}").status_code == 200
        assert client.get("/api/reminders").json() == []


# ---------------------------------------------------------------- Briefing

def test_briefing_combines_sources(cfg, fake_weather, monkeypatch):
    cfg.weather.location = "Freiburg"
    c = ctx(cfg)
    reminder_tools.store_for(c).add("Paket abholen", datetime.now().replace(hour=23, minute=59))

    async def fake_run(ctx_, argv, timeout, stream=True, cwd=None):
        return 0, "linux 6.18.1-1 -> 6.18.2-1\nfirefox 143.0-1 -> 144.0-1\nvim 9.1-1 -> 9.1-2\n"

    monkeypatch.setattr(briefing.shutil, "which", lambda n: "/usr/bin/checkupdates")
    monkeypatch.setattr(proc, "run", fake_run)
    out = run(briefing.daily_briefing(c))
    assert out.startswith("Heute ist ")
    assert "Freiburg im Breisgau" in out
    assert "23:59 Paket abholen" in out
    assert "Systemupdates: 3 verfügbar (darunter linux, firefox)." in out


def test_briefing_survives_missing_sources(cfg, monkeypatch):
    monkeypatch.setattr(briefing.shutil, "which", lambda n: None)
    out = run(briefing.daily_briefing(ctx(cfg)))
    assert "kein Standardort" in out and "keine Erinnerungen" in out


def test_searxng_403_falls_back_to_duckduckgo(cfg, monkeypatch):
    cfg.tools.searxng_url = "https://searx.example"
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden")))
    monkeypatch.setattr(web, "_ddgs", lambda q, n: [{"title": "Ryzen AI Max", "href": "https://amd.com", "body": "Neu"}])
    out = run(web.web_search(ctx(cfg), "Ryzen AI Max neues"))
    assert "HTTP 403" in out and "DuckDuckGo" in out
    assert "1. Ryzen AI Max\nhttps://amd.com" in out


def test_searxng_success(cfg, monkeypatch):
    cfg.tools.searxng_url = "http://localhost:8888"
    payload = {"results": [{"title": "T", "url": "https://t.de", "content": "C"}]}
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    monkeypatch.setattr(web, "_ddgs", lambda q, n: pytest.fail("DuckDuckGo darf nicht genutzt werden"))
    assert run(web.web_search(ctx(cfg), "x")) == "1. T\nhttps://t.de\nC"


def test_brave_api_is_used_with_key(cfg, monkeypatch):
    cfg.tools.brave_api_key = "secret"
    seen = []

    def handler(r):
        seen.append(r)
        return httpx.Response(200, json={"web": {"results": [
            {"title": "<strong>Ryzen</strong> AI", "url": "https://amd.com", "description": "Neu <b>2026</b>"}]}})

    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(web, "_ddgs", lambda q, n: pytest.fail("Suchseiten dürfen nicht ausgelesen werden"))
    assert run(web.web_search(ctx(cfg), "Ryzen AI", 3)) == "1. Ryzen AI\nhttps://amd.com\nNeu 2026"
    r = seen[0]
    assert str(r.url).startswith(web.BRAVE_URL) and r.headers["X-Subscription-Token"] == "secret"
    assert r.url.params["q"] == "Ryzen AI" and r.url.params["count"] == "3" and r.url.params["country"] == "DE"
    cfg.language = "en"
    run(web.web_search(ctx(cfg), "x"))
    assert seen[1].url.params["search_lang"] == "en" and "country" not in seen[1].url.params


@pytest.mark.parametrize("status,body,text", [(429, "", "Rate-Limit"), (401, "", "ungültig"),
                                              (422, '{"error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}}', "ungültig")])
def test_brave_errors_do_not_scrape(cfg, monkeypatch, status, body, text):
    cfg.tools.brave_api_key = "secret"
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(status, text=body)))
    monkeypatch.setattr(web, "_ddgs", lambda q, n: pytest.fail("Suchseiten dürfen nicht ausgelesen werden"))
    out = run(web.web_search(ctx(cfg), "x"))
    assert out.startswith("Websuche fehlgeschlagen") and text in out


def test_brave_fallback_when_enabled(cfg, monkeypatch):
    monkeypatch.setenv("JARVIS_BRAVE_API_KEY", "from-env")
    cfg.tools.search_fallback = True
    assert cfg.tools.brave_key == "from-env"
    monkeypatch.setattr(web, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(429)))
    monkeypatch.setattr(web, "_ddgs", lambda q, n: [{"title": "T", "href": "https://t.de", "body": "C"}])
    out = run(web.web_search(ctx(cfg), "x"))
    assert "HTTP 429" in out and "DuckDuckGo" in out and out.endswith("1. T\nhttps://t.de\nC")
