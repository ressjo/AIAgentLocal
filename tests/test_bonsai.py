import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from orbwise import bonsai
from orbwise import models as mdl
from orbwise.config import LLMConfig
from orbwise.llm_router import LLMRouter

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
    assert "ORBWISE_BONSAI_DIR" in msgs[-1]
    target = tmp_path / "b2"
    failing = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1 if cmd[0] == "sh" else 0)  # noqa: E731
    assert not bonsai.setup(target, run=failing, out=msgs.append) and "fehlgeschlagen" in msgs[-1]


def test_preset_and_web_pull_refuses_bonsai(cfg, monkeypatch):
    assert mdl.BY_TAG["bonsai"].kind == "bonsai"
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    monkeypatch.delenv("ORBWISE_FAKE_LLM", raising=False)
    cfg.llm.base_url = "http://127.0.0.1:9"
    from orbwise.server import create_app
    with TestClient(create_app(cfg), base_url="http://localhost:8765") as client:
        r = client.post("/api/models/pull", json={"tag": "bonsai"})
        assert r.status_code == 400 and "orbwise model add bonsai" in r.json()["detail"]


LDD_MISSING = """\tlinux-vdso.so.1 (0x00007ffc)
\tlibcudart.so.12 => not found
\tlibcublas.so.12 => not found
\tlibc.so.6 => /usr/lib/libc.so.6 (0x00007f)
"""


def cuda_checkout(tmp_path):
    d = tmp_path / "bonsai"
    (d / "bin" / "cuda").mkdir(parents=True)
    (d / bonsai.CUDA_SERVER).write_text("")
    return d


def fake_cuda_run(d, libs_after_install=True, calls=None):
    calls = [] if calls is None else calls

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "nvidia-smi":
            return subprocess.CompletedProcess(cmd, 0, stdout="| NVIDIA-SMI 570.1  Driver Version: 570.1  CUDA Version: 12.8 |")
        if cmd[0] == "ldd":
            path = kw["env"].get("LD_LIBRARY_PATH", "")
            ok = "cuda_runtime/lib" in path and "cublas/lib" in path
            return subprocess.CompletedProcess(cmd, 0, stdout="\tlibc.so.6 => /usr/lib/libc.so.6\n" if ok else LDD_MISSING)
        if "--target" in cmd and libs_after_install:
            target = Path(cmd[cmd.index("--target") + 1])
            for sub, lib in (("cuda_runtime", "libcudart.so.12"), ("cublas", "libcublas.so.12")):
                (target / "nvidia" / sub / "lib").mkdir(parents=True, exist_ok=True)
                (target / "nvidia" / sub / "lib" / lib).write_text("")
        return subprocess.CompletedProcess(cmd, 0)

    return run, calls


def test_cuda_runtime_is_downloaded_when_missing(tmp_path):
    d = cuda_checkout(tmp_path)
    run, calls = fake_cuda_run(d)
    msgs = []
    path = bonsai.ensure_cuda_libs(d, run=run, out=msgs.append)
    pip = next(c for c in calls if "--target" in c)
    assert pip[-2:] == ["nvidia-cuda-runtime-cu12<12.9", "nvidia-cublas-cu12<12.9"] and str(d / "cuda-libs") in pip
    assert "nvidia/cuda_runtime/lib" in path and "nvidia/cublas/lib" in path and "✔" in msgs[-1]
    # zweiter Lauf: Bibliotheken schon da → kein erneuter Download
    run2, calls2 = fake_cuda_run(d)
    assert bonsai.ensure_cuda_libs(d, run=run2, out=msgs.append) == path
    assert not any("--target" in c for c in calls2)
    assert "LD_LIBRARY_PATH" in bonsai.make_profile(NVIDIA_16GB, d, lib_path=path)["server"]["env"]


def test_cuda_runtime_not_needed_or_failing(tmp_path):
    assert bonsai.ensure_cuda_libs(tmp_path, run=None, out=print) == ""  # kein CUDA-Build (z. B. AMD)
    d = cuda_checkout(tmp_path)
    ok = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="\tlibc.so.6 => /usr/lib/libc.so.6\n")  # noqa: E731
    assert bonsai.ensure_cuda_libs(d, run=ok, out=print) == ""  # System-CUDA vorhanden
    run, _ = fake_cuda_run(d, libs_after_install=False)
    msgs = []
    assert bonsai.ensure_cuda_libs(d, run=run, out=msgs.append) is None and "libcudart.so.12" in msgs[-1]
    assert bonsai.cuda_packages(["libcudart.so.13"]) == ["nvidia-cuda-runtime==13.*", "nvidia-cublas==13.*"]
    assert bonsai.cuda_packages(["libfoo.so.1"]) == []
    assert bonsai.cuda_packages(["libcudart.so.13"], (13, 0)) == ["nvidia-cuda-runtime>=13,<13.1", "nvidia-cublas>=13,<13.1"]


def test_reinstall_keeps_api_key_and_adds_lib_path(tmp_path, monkeypatch):
    monkeypatch.setattr(bonsai.shutil, "which", lambda n: "/usr/bin/" + n)
    state = tmp_path / "state.json"
    d = tmp_path / "bonsai"
    run, _ = fake_run(d)
    bonsai.install(state, gpu=NVIDIA_16GB, directory=d, run=run, out=lambda *_: None)
    key = mdl.added_profiles(state)["bonsai"]["api_key"]
    (d / "bin" / "cuda").mkdir(parents=True)
    (d / bonsai.CUDA_SERVER).write_text("")
    cuda_run, _ = fake_cuda_run(d)

    def both(cmd, **kw):
        return cuda_run(cmd, **kw) if cmd[0] in ("ldd", "nvidia-smi") or "--target" in cmd else run(cmd, **kw)

    bonsai.install(state, gpu=NVIDIA_16GB, directory=d, run=both, out=lambda *_: None)
    p = mdl.added_profiles(state)["bonsai"]
    assert p["api_key"] == key and key in p["server"]["command"] and "cublas" in p["server"]["env"]["LD_LIBRARY_PATH"]


def test_router_error_names_missing_library(tmp_path, monkeypatch):
    from orbwise import llm_router
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    log = tmp_path / "orbwise-llm.log"
    log.write_text("\n===== Starte: old\nerror while loading shared libraries: libold.so.1: x\n"
                   "\n===== Starte: ~/bonsai/scripts/start_llama_server.sh\n"
                   "llama-server: error while loading shared libraries: libcudart.so.12: cannot open shared object file\n")
    hint = llm_router.library_hint("~/bonsai/scripts/start_llama_server.sh -np 1")
    assert "libcudart.so.12" in hint and "orbwise model add bonsai" in hint and "libold" not in hint
    log.write_text("\n===== Starte: x\nall good\n")
    assert llm_router.library_hint("x") == ""
