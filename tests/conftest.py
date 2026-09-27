import asyncio
from pathlib import Path

import pytest

from jarvis.config import Config
from jarvis.llm import FakeLLM
from jarvis.memory import Memory


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = Config()
    c.memory.dir = tmp_path / "memory"
    c.tools.search_paths = [tmp_path]
    c.voice.enabled = False
    return c


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM(delay=0)


@pytest.fixture
def memory(cfg, llm):
    m = Memory(cfg.memory, llm)
    yield m
    m.close()


def run(coro):
    return asyncio.run(coro)
