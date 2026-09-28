import json

import httpx
from conftest import run

from orbwise.config import LLMConfig
from orbwise.llm import LLMError, OllamaLLM


def make_llm(handler) -> OllamaLLM:
    llm = OllamaLLM(LLMConfig())
    llm._client = httpx.AsyncClient(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return llm


def test_stream_with_tool_calls():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        lines = [
            {"message": {"role": "assistant", "content": "Einen "}, "done": False},
            {"message": {"role": "assistant", "content": "Moment."}, "done": False},
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "system_update", "arguments": {}}}]}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ]
        return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))

    llm = make_llm(handler)

    async def go():
        return [ev async for ev in llm.chat_stream([{"role": "user", "content": "x"}], [{"type": "function"}])]

    events = run(go())
    assert [e["text"] for e in events if e["type"] == "token"] == ["Einen ", "Moment."]
    final = events[-1]["message"]
    assert final["content"] == "Einen Moment."
    assert final["tool_calls"][0]["function"]["name"] == "system_update"
    assert seen["body"]["think"] is False and seen["body"]["tools"]


def test_error_is_raised():
    llm = make_llm(lambda r: httpx.Response(404, text='{"error":"model not found"}'))

    async def go():
        return [ev async for ev in llm.chat_stream([{"role": "user", "content": "x"}])]

    try:
        run(go())
    except LLMError as e:
        assert "404" in str(e)
    else:
        raise AssertionError("LLMError erwartet")


def test_chat_strips_think():
    llm = make_llm(lambda r: httpx.Response(200, json={"message": {"content": "<think>hmm</think>Antwort"}}))
    assert run(llm.chat([{"role": "user", "content": "x"}])) == "Antwort"
