"""Telegram-Bot: nur der eigene Chat, Antworten, Rückfragen per Knopf, Sprachnachrichten, Erinnerungen."""

import asyncio

import pytest
from conftest import run
from fake_telegram import TOKEN, FakeTelegram, wait_for
from fastapi.testclient import TestClient

from orbwise.telegram import TelegramBot, split_text

ME = 4242


@pytest.fixture
def tg(cfg):
    cfg.telegram.token, cfg.telegram.chat_id = TOKEN, ME
    return FakeTelegram()


def make_bot(cfg, fake, run_fn, transcribe=None):
    return TelegramBot(cfg, run_fn, lambda n, a: f"die Aktion {n}", lambda n: n == "mail_send",
                       transcribe=transcribe, transport=fake.transport, poll_timeout=0)


def test_split_text():
    parts = split_text("a" * 3000 + "\n\n" + "b" * 3000, limit=4000)
    assert parts == ["a" * 3000, "b" * 3000]  # an Absätzen geteilt
    parts = split_text("x" * 9000, limit=4000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]


def test_only_own_chat_is_served(cfg, tg):
    asked = []

    async def run_fn(text, emit, confirm):
        asked.append(text)
        return f"Antwort auf {text}"

    async def scenario():
        bot = make_bot(cfg, tg, run_fn)
        worker = asyncio.create_task(bot._worker())
        tg.user_message(999, "Wie spät ist es?")          # fremder Chat
        tg.user_message(ME, "/start")
        tg.user_message(ME, "Wie wird das Wetter?")
        await bot.poll_once()
        await wait_for(lambda: any("Antwort auf" in t for t in tg.texts()))
        worker.cancel()

    run(scenario())
    assert asked == ["Wie wird das Wetter?"]  # nur der eigene Chat, /start ist keine Anfrage
    stranger = next(m for m in tg.sent if m["chat_id"] == 999)
    assert "nicht freigeschaltet" in stranger["text"] and "999" in stranger["text"]
    mine = [m["text"] for m in tg.sent if m["chat_id"] == ME]
    assert "Schreib oder sprich" in mine[0] and mine[-1] == "Antwort auf Wie wird das Wetter?"


def test_confirmation_buttons(cfg, tg):
    results = []

    async def run_fn(text, emit, confirm):
        results.append(await confirm("c1", "install_package", {"names": "htop"}, ""))
        results.append(await confirm("c2", "mail_send", {"to": "a@b.de"}, ""))  # nur im Dashboard
        return "fertig"

    async def scenario():
        bot = make_bot(cfg, tg, run_fn)
        worker = asyncio.create_task(bot._worker())
        tg.user_message(ME, "Installier htop")
        await bot.poll_once()
        await wait_for(lambda: tg.buttons())
        ok, no = tg.buttons()
        assert ok.startswith("ok:") and no.startswith("no:")
        tg.press(999, ok)          # fremder Knopfdruck zählt nicht
        await bot.poll_once()
        await asyncio.sleep(0.05)
        assert not results
        tg.press(ME, ok)
        await bot.poll_once()
        await wait_for(lambda: "fertig" in tg.texts())
        worker.cancel()

    run(scenario())
    assert results == [True, False]
    assert "Soll ich die Aktion install_package ausführen?" in next(m["text"] for m in tg.sent if "reply_markup" in m)
    assert tg.edited and "ausgeführt" in tg.edited[-1]["text"]
    assert any("Das geht nur am PC" in t for t in tg.texts())


def test_voice_message_is_transcribed(cfg, tg):
    asked = []

    async def run_fn(text, emit, confirm):
        asked.append(text)
        return "ok"

    async def transcribe(data):
        assert data == b"OggS-fake"
        return "Erinner mich in zehn Minuten an den Tee"

    async def scenario():
        bot = make_bot(cfg, tg, run_fn, transcribe)
        worker = asyncio.create_task(bot._worker())
        tg.user_message(ME, voice="voice-1")
        await bot.poll_once()
        await wait_for(lambda: "ok" in tg.texts())
        worker.cancel()

    run(scenario())
    assert asked == ["Erinner mich in zehn Minuten an den Tee"]
    assert "🎙 „Erinner mich in zehn Minuten an den Tee“" in tg.texts()


def test_notify_needs_setup(cfg, tg):
    cfg.telegram.chat_id = 0
    bot = make_bot(cfg, tg, None)
    assert run(bot.notify("x")) is False and not tg.sent


def test_server_telegram_chat_confirm_and_reminder(cfg, tg, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.setattr(TelegramBot, "TRANSPORT", tg.transport)
    from orbwise.server import create_app
    target = tmp_path / "notiz.txt"
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        memory = client.app.state.memory
        active_before = memory.conversation.chat_id

        def until(cond, timeout=8.0):
            client.portal.call(wait_for, cond, timeout)

        # Frage vom Handy → Antwort im eigenen Chat „📱 Telegram“
        tg.user_message(ME, "Hallo vom Handy")
        until(lambda: any("Hallo vom Handy" in t for t in tg.texts()))
        titles = [c["title"] for c in memory.chats.list()]
        assert "📱 Telegram" in titles and memory.conversation.chat_id == active_before
        # Aktion mit Rückfrage → Knopf am Handy
        tg.user_message(ME, f'/tool write_file {{"path": "{target}", "content": "vom Handy"}}')
        until(lambda: tg.buttons())
        tg.press(ME, tg.buttons()[0])
        until(lambda: target.exists())
        assert target.read_text() == "vom Handy"
        # Erinnerung → kommt aufs Handy
        tg.user_message(ME, '/tool set_reminder {"text": "Tee holen", "in_minutes": 0.02}')
        until(lambda: any(t.startswith("⏰") and "Tee holen" in t for t in tg.texts()), timeout=10)


def test_startup_check_and_errors_are_reported(cfg, tg, monkeypatch):
    from orbwise import telegram

    async def scenario():
        bot = make_bot(cfg, tg, None)
        await bot.check()
        return bot

    bot = run(scenario())
    assert bot.status["bot"] == "@orbwise_test_bot" and tg.webhook_deleted
    assert "Token ungültig" in telegram.explain("Telegram getMe: Unauthorized")
    assert "anderes Programm" in telegram.explain("Conflict: terminated by other getUpdates request")
    assert "nicht erreichbar" in telegram.explain("ConnectError: [Errno -3] Temporary failure in name resolution")

    # falscher Token: der Bot läuft weiter, meldet den Fehler im Status statt still zu sterben
    cfg.telegram.token = "999:FALSCH"

    async def broken():
        bad = TelegramBot(cfg, None, lambda n, a: n, lambda n: False, transport=tg.transport, poll_timeout=0)
        task = asyncio.create_task(bad.serve())
        await wait_for(lambda: bad.status["error"])
        task.cancel()
        return bad.status

    status = run(broken())
    assert not status["running"] and "Token ungültig" in status["error"]
