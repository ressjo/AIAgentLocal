import asyncio

from conftest import run

from jarvis.agent import Agent, ThinkFilter


def make_agent(cfg, llm, memory):
    return Agent(cfg, llm, memory)


async def collect(agent, text, approve=None):
    events = []
    confirms = []

    async def emit(e):
        events.append(e)

    async def confirm(call_id, name, args, reason):
        confirms.append((name, args, reason))
        return approve

    answer = await agent.run(text, emit, confirm)
    return answer, events, confirms


def test_plain_answer_logged_to_journal(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)
    answer, events, _ = run(collect(agent, "Hallo Jarvis"))
    assert "Hallo Jarvis" in answer
    assert any(e["type"] == "token" for e in events)
    assert events[-1]["type"] == "assistant_end"
    day = memory.journal.days()[0]
    assert "Hallo Jarvis" in memory.journal.read(day)


def test_safe_tool_runs_without_confirmation(cfg, llm, memory, tmp_path):
    (tmp_path / "rechnung_2026.pdf").write_text("x")
    agent = make_agent(cfg, llm, memory)
    answer, events, confirms = run(collect(agent, f'/tool find_files {{"query": "rechnung", "path": "{tmp_path}"}}'))
    assert not confirms
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["status"] == "ok" and "rechnung_2026.pdf" in result["text"]
    assert answer.startswith("Erledigt")


def test_risky_tool_requires_confirmation(cfg, llm, memory, tmp_path):
    target = tmp_path / "x.txt"
    agent = make_agent(cfg, llm, memory)
    cmd = f'/tool run_shell {{"command": "touch {target}"}}'
    _, events, confirms = run(collect(agent, cmd, approve=False))
    assert confirms and confirms[0][0] == "run_shell"
    assert not target.exists()
    assert next(e for e in events if e["type"] == "tool_result")["status"] == "denied"

    _, events, _ = run(collect(agent, cmd, approve=True))
    assert target.exists()


def test_blocked_command_never_runs(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)
    _, events, confirms = run(collect(agent, '/tool run_shell {"command": "rm -rf ~"}', approve=True))
    assert not confirms
    assert next(e for e in events if e["type"] == "tool_result")["status"] == "blocked"


def test_remember_tool_and_context(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)
    run(collect(agent, '/tool remember {"fact": "Der Nutzer heißt Joshua."}'))
    msgs = agent.build_messages([])
    assert "Der Nutzer heißt Joshua." in msgs[0]["content"]


def test_history_consistent_after_cancel(cfg, llm, memory):
    agent = make_agent(cfg, llm, memory)

    async def scenario():
        async def emit(e):
            pass

        async def confirm(*a):
            await asyncio.sleep(10)
            return True

        task = asyncio.create_task(agent.run('/tool run_shell {"command": "touch /tmp/jarvis-x"}', emit, confirm))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run(scenario())
    hist = memory.conversation.history
    assert hist[-2]["role"] == "tool" and hist[-2]["content"] == "[abgebrochen]"
    assert hist[-1]["role"] == "assistant"


def test_think_filter():
    f = ThinkFilter()
    out = "".join(f.feed(t) for t in ["Hal", "lo <thi", "nk>geheim</th", "ink> Welt"]) + f.flush()
    assert out == "Hallo  Welt"
