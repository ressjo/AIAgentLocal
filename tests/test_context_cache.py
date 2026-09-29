"""Kontext-Optimierung: stabiler Prompt-Anfang (KV-Cache), Alterung langer Tool-Ergebnisse, kalibrierte Schätzung."""

from datetime import datetime

from conftest import run

from orbwise import prompts
from orbwise.agent import Agent
from orbwise.memory.context import AGED_NOTE, Conversation
from orbwise.memory.index import Hit


def hit(text: str) -> Hit:
    return Hit(id=1, kind="journal", day="2026-09-20", text=text, created=0.0, score=1.0)


def prefix(messages: list[dict]) -> list[dict]:
    """Alles vor der letzten Nutzernachricht – das kann der Modell-Server aus dem Cache nehmen."""
    last = max(i for i, m in enumerate(messages) if m["role"] == "user")
    return messages[:last]


def test_prompt_prefix_stays_identical_across_time_and_memories(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    conv = memory.conversation
    conv.add({"role": "user", "content": "Wie heißt mein NAS?"})
    conv.add({"role": "assistant", "content": "Tresor."})
    conv.add({"role": "user", "content": "Und welche IP hat es?"})
    agent._turn_time = "14:03"
    first = agent.build_messages([hit("NAS Tresor unter 10.0.0.5")])
    agent._turn_time = "14:07"
    second = agent.build_messages([hit("Router unter 10.0.0.1")])
    assert prefix(first) == prefix(second)  # System-Prompt + früherer Verlauf unverändert
    assert "14:0" not in first[0]["content"] and "10.0.0" not in first[0]["content"]
    assert first[-1]["content"].startswith("[Kontext – nicht vom Nutzer geschrieben: Uhrzeit 14:03]")
    assert "NAS Tresor unter 10.0.0.5" in first[-1]["content"] and first[-1]["content"].endswith("Und welche IP hat es?")
    assert "Router unter 10.0.0.1" in second[-1]["content"]
    assert [m["content"] for m in conv.history if m["role"] == "user"][-1] == "Und welche IP hat es?"  # nicht gespeichert
    assert prompts.strip_context_note(second[-1]["content"]) == "Und welche IP hat es?"


def test_agent_answer_ignores_context_note(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)

    async def emit(ev):
        pass

    async def confirm(*a):
        return False

    answer = run(agent.run("Hallo Jarvis", emit, confirm))
    assert "Hallo Jarvis" in answer and "Kontext" not in answer
    assert memory.conversation.history[0]["content"] == "Hallo Jarvis"


def test_old_tool_results_are_aged_but_latest_turn_stays_complete():
    conv = Conversation()
    long = "Zeile\n" * 600  # ~3600 Zeichen
    conv.add({"role": "user", "content": "Lies die Seite"})
    conv.add({"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "fetch_url", "arguments": {}}}]})
    conv.add({"role": "tool", "content": long, "tool_name": "fetch_url"})
    conv.add({"role": "tool", "content": "kurz", "tool_name": "system_info"})
    conv.add({"role": "assistant", "content": "Fertig."})
    assert conv.age_tool_results() == 0  # nur eine Runde → bleibt vollständig (Nachfragen)
    conv.add({"role": "user", "content": "Und jetzt die zweite Seite"})
    conv.add({"role": "tool", "content": long, "tool_name": "fetch_url"})
    assert conv.age_tool_results() == 1
    old, short, new = conv.history[2]["content"], conv.history[3]["content"], conv.history[6]["content"]
    assert old.endswith(AGED_NOTE) and len(old) < 1000 and short == "kurz" and new == long
    assert conv.age_tool_results() == 0  # einmalig – der Anfang bleibt danach stabil


def test_compaction_starts_before_the_budget_is_full(tmp_path, llm):
    conv = Conversation(tmp_path / "c.json")
    for i in range(12):
        conv.add({"role": "user", "content": f"Frage {i} " + "x" * 300})
        conv.add({"role": "assistant", "content": f"Antwort {i} " + "y" * 300})
    total = conv.history_tokens()
    assert run(conv.compact(llm, int(total / 0.9)))  # 90 % belegt → verdichten
    assert conv.running_summary and conv.history_tokens() < total
    conv2 = Conversation(tmp_path / "d.json")
    conv2.add({"role": "user", "content": "kurz"})
    assert not run(conv2.compact(llm, 10_000))


def test_token_estimate_learns_from_server(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    base = agent.context_budget()
    agent.learn_tokens(4000, 5000)  # Server zählt mehr Token als geschätzt → Budget vorsichtiger
    assert agent.token_ratio() == 1.25 and agent.context_budget() < base
    for _ in range(20):
        agent.learn_tokens(4000, 2000)  # weit darunter → Faktor sinkt, aber nicht unter 0,6
    assert agent.token_ratio() >= 0.6 and agent.context_budget() > base
    agent.learn_tokens(100, 5000)  # zu kleine Stichprobe → ignoriert
    assert agent.token_ratio() >= 0.6


def test_context_event_reports_cache_and_condensed(cfg, memory, llm):
    agent = Agent(cfg, llm, memory)
    events = []

    async def emit(ev):
        events.append(ev)

    async def confirm(*a):
        return False

    original = llm.chat_stream

    async def with_cache(messages, tools=None, **kw):
        async for ev in original(messages, tools, **kw):
            if ev["type"] == "done":
                ev = {**ev, "stats": {**ev.get("stats", {}), "prompt_total": 900, "prompt_cached": 850}}
            yield ev

    llm.chat_stream = with_cache
    memory.conversation.running_summary = "- Nutzer richtet Bonsai ein"
    run(agent.run("Wie geht's?", emit, confirm))
    ctx = [e for e in events if e["type"] == "context"][-1]
    assert ctx["real"] == 900 and ctx["cached"] == 850 and ctx["summarized"] is True
    assert datetime.now().strftime("%H:") in agent._turn_time
