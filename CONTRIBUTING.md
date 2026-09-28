# Contributing

Thanks for helping to make Jarvis better! Bug reports, new tools, translations and documentation are all welcome.

## Development setup

```bash
git clone https://github.com/ressjo/AIAgentLocal.git && cd AIAgentLocal
uv sync --extra dev --extra voice
uv run pytest -q                 # all tests run offline, no GPU or Ollama needed
uv run ruff check jarvis tests
JARVIS_FAKE_LLM=1 uv run jarvis serve --open   # UI demo with a fake model
```

## Project layout

| Path | What lives there |
|---|---|
| `jarvis/agent.py` | The agent loop: prompt building, streaming, tool calls, confirmations, step limit |
| `jarvis/prompts.py` | System prompt and fixed texts (German + English) |
| `jarvis/llm.py`, `jarvis/llm_router.py` | Ollama and OpenAI-compatible clients, model profiles, managed llama-server |
| `jarvis/memory/` | Journal, summaries, facts, chats, search index, context budget |
| `jarvis/tools/` | All tools – one module per group |
| `jarvis/server.py` | FastAPI app, WebSocket hub, REST API |
| `jarvis/web/` | The web UI (vanilla JS, no build step) |
| `scripts/` | Installer, launcher, desktop files |

## Writing a new tool

Tools are plain async functions registered with the `@tool` decorator; the JSON schema for the model is built
from the type annotations:

```python
from typing import Annotated
from .registry import CONFIRM, ToolContext, tool

@tool("Restarts the Docker container with the given name.",
      risk=lambda ctx, args: (CONFIRM, f"restart container {args.get('name')}"))
async def restart_container(ctx: ToolContext, name: Annotated[str, "container name"]) -> str:
    ...
    return "Done."
```

- Put it in a module under `jarvis/tools/` and add the module to `load_all_tools()` in `registry.py`.
- Anything that changes the system must use `CONFIRM` (or a risk function); never run user input through a
  shell without validation – prefer argument lists with `proc.run(ctx, [...])`.
- Integrations that need configuration use `enabled=` so they only appear when configured.
- Add tests in `tests/` (mock external services with `httpx.MockTransport`, see `tests/fake_*.py`).
- If the tool belongs to a large optional group, add keywords for it in `jarvis/toolselect.py`.

## Pull requests

- Keep changes focused, add tests, make sure `pytest` and `ruff` pass.
- User-visible texts exist in German and English (`prompts.py`, `web_i18n.py`, `L("…", "…")` in `app.js`,
  `T("…", "…")` in the CLI).
