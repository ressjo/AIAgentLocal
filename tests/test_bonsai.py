import subprocess

from fastapi.testclient import TestClient

from jarvis import bonsai
from jarvis import models as mdl
from jarvis.config import LLMConfig
from jarvis.llm_router import LLMRouter

AMD_8GB = {"vendor": "amd", "name": "Navi 23 [Radeon RX 6650 XT / 6700S / 6800S]", "vram_gb": 8.0}
NVIDIA_16GB = {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 4080 SUPER", "vram_gb": 16.0}


def test_profile_for_small_amd_card(tmp_path):
    p = bonsai.make_profile(AMD_8GB, tmp_path / "bonsai")
    env = p["server"]["env"]
    assert env == {"HSA_OVERRIDE_GFX_VERSION": "10.3.0", "BONSAI_CTX": "8192", "BONSAI_KV4": "1",
                   "BONSAI_MMPROJ_CPU": "1"}
    assert p["embed_on_cpu"] and p["unload_ollama"] and p["backend"] == "openai"
    assert p["server"]["command"].endswith(f"-np 1 --api-key {p['api_key']}") and len(p["api_key"]) >= 32 and not p["api_key"].startswith("-")


def test_profile_for_big_nvidia_card(tmp_path):
    p = bonsai.make_profile(NVIDIA_16GB, tmp_path / "bonsai", api_key="k")
    assert p["server"]["env"] == {"BONSAI_CTX": "65536"} and not p["embed_on_cpu"]
    assert bonsai.hsa_override("Navi 33 [Radeon RX 7600]") == "11.0.0" and bonsai.hsa_override("RTX 4080") == ""


def fake_run(target):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "clone"]:
            (target / ".git").mkdir(parents=True)
            (target / "scripts").mkdir()
            (target / "setup.sh").write_text("#!/bin/sh\n")
        if cmd == ["sh", "./setup.sh"]:
            assert kw["env"]["BONSAI_OPENWEBUI"] == "0" and kw["cwd"] == str(target)
            (target / bonsai.START_SCRIPT).write_text("#!/bin/sh\n")
            (target / "models" / "gguf").mkdir(parents=True, exist_ok=True)
            (target / "models" / "gguf" / "Bonsai-2-27B-PQ2_0.gguf").write_text("x")
        return subprocess.CompletedProcess(cmd, 0)

    return run, calls


def test_install_registers_and_router_uses_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(bonsai.shutil, "which", lambda n: "/usr/bin/git")
    target, state = tmp_path / "bonsai", tmp_path / "state.json"
    run, calls = fake_run(target)
    assert not bonsai.is_set_up(target)
    assert bonsai.install(state, gpu=AMD_8GB, directory=target, run=run, out=lambda *_: None) == "bonsai"
    assert calls[0][:2] == ["git", "clone"] and calls[1] == ["sh", "./setup.sh"]
    assert bonsai.is_set_up(target)
    router = LLMRouter(LLMConfig(model="qwen3:8b"), state_path=state)
    assert router.active == "bonsai"
    p = router.profile
    assert p.backend == "openai" and p.server and p.server.env["BONSAI_KV4"] == "1" and p.api_key in p.server.command
    # zweiter Aufruf: vorhandenes Repo wird aktualisiert statt neu geklont
    run2, calls2 = fake_run(target)
    bonsai.setup(target, run=run2, out=lambda *_: None)
    assert calls2[0] == ["git", "-C", str(target), "pull", "--ff-only"]
    assert mdl.unregister_model(state, "bonsai") == "bonsai"


def test_setup_failure_and_foreign_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(bonsai.shutil, "which", lambda n: "/usr/bin/git")
    msgs = []
    foreign = tmp_path / "bonsai"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("x")
    assert not bonsai.setup(foreign, run=lambda *a, **k: None, out=msgs.append)
    assert "JARVIS_BONSAI_DIR" in msgs[-1]
    target = tmp_path / "b2"
    failing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1 if cmd[0] == "sh" else 0)  # noqa: E731
    assert not bonsai.setup(target, run=failing, out=msgs.append) and "fehlgeschlagen" in msgs[-1]


def test_preset_and_web_pull_refuses_bonsai(cfg, monkeypatch):
    assert mdl.BY_TAG["bonsai"].kind == "bonsai"
    monkeypatch.setenv("JARVIS_SKIP_WARMUP", "1")
    monkeypatch.delenv("JARVIS_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    from jarvis.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        r = client.post("/api/models/pull", json={"tag": "bonsai"})
        assert r.status_code == 400 and "jarvis model add bonsai" in r.json()["detail"]
