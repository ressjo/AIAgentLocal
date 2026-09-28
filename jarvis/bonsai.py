"""Bonsai 2 27B automatisch einrichten: offizielles Bonsai-demo-Repo (PrismML-Fork von llama.cpp + Modell-Download)
nach ~/bonsai holen, `setup.sh` ausführen und ein Jarvis-Profil anlegen, das den llama-server selbst startet.

Bonsai 2 läuft nur mit den llama.cpp-Binaries aus dem PrismML-Fork – `setup.sh` lädt die passenden für
CUDA/ROCm/Vulkan/CPU. Die Modelle sind öffentlich (kein Hugging-Face-Token nötig).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .lang import T

REPO = "https://github.com/PrismML-Eng/Bonsai-demo.git"
START_SCRIPT = "scripts/start_llama_server.sh"
PROFILE_NAME = "bonsai"


def bonsai_dir() -> Path:
    return Path(os.environ.get("JARVIS_BONSAI_DIR", Path.home() / "bonsai")).expanduser()


def is_set_up(directory: Path | None = None) -> bool:
    d = directory or bonsai_dir()
    models = d / "models"
    return (d / START_SCRIPT).exists() and models.is_dir() and any(models.rglob("*.gguf"))


def hsa_override(gpu_name: str) -> str:
    """RDNA2/RDNA3-Karten ohne offiziellen ROCm-Support (RX 6600/6650/6700, RX 7600) brauchen einen Override."""
    if re.search(r"navi 2[234]", gpu_name, re.I):
        return "10.3.0"
    if re.search(r"navi 3[23]", gpu_name, re.I):
        return "11.0.0"
    return ""


def make_profile(gpu: dict, directory: Path, api_key: str | None = None) -> dict:
    """Profil passend zur Grafikkarte: wenig VRAM → komprimierter KV-Cache, Bildmodul in den RAM, kleiner Kontext."""
    vram = float(gpu.get("vram_gb") or 0)
    key = api_key or secrets.token_hex(20)
    env: dict[str, str] = {}
    if gpu.get("vendor") == "amd" and hsa_override(gpu.get("name", "")):
        env["HSA_OVERRIDE_GFX_VERSION"] = hsa_override(gpu["name"])
    if vram <= 9:
        env.update({"BONSAI_CTX": "8192", "BONSAI_KV4": "1", "BONSAI_MMPROJ_CPU": "1"})
    elif vram <= 13:
        env.update({"BONSAI_CTX": "32768", "BONSAI_MMPROJ_CPU": "1"})
    else:
        env["BONSAI_CTX"] = "65536"
    tight = vram <= 13
    home = str(Path.home())
    script = str(directory / START_SCRIPT)
    if script.startswith(home + "/"):
        script = "~" + script[len(home):]
    return {
        "label": "Bonsai 2 27B",
        "backend": "openai",
        "base_url": "http://127.0.0.1:8080/v1",
        "model": "bonsai",
        "api_key": key,
        "embed_on_cpu": tight,
        "unload_ollama": tight,
        "server": {"command": f"{script} -np 1 --api-key {key}", "env": env, "startup_timeout": 300},
    }


Runner = Callable[..., subprocess.CompletedProcess]


def setup(directory: Path | None = None, run: Runner = subprocess.run, out=print) -> bool:
    """Repo holen/aktualisieren und setup.sh ausführen (lädt Binaries und ~7 GB Modell)."""
    d = directory or bonsai_dir()
    if not shutil.which("git"):
        out(T("git fehlt – bitte installieren.", "git is missing – please install it."))
        return False
    if (d / ".git").exists():
        out(T(f"Aktualisiere {d} …", f"Updating {d} …"))
        run(["git", "-C", str(d), "pull", "--ff-only"])
    elif d.exists() and any(d.iterdir()):
        out(T(f"{d} existiert bereits und ist kein Bonsai-demo-Checkout – bitte umbenennen oder JARVIS_BONSAI_DIR setzen.",
              f"{d} already exists and is not a Bonsai-demo checkout – rename it or set JARVIS_BONSAI_DIR."))
        return False
    else:
        out(T(f"Lade Bonsai-demo nach {d} …", f"Cloning Bonsai-demo to {d} …"))
        if run(["git", "clone", "--depth", "1", REPO, str(d)]).returncode != 0:
            return False
    out(T("Richte Bonsai ein (llama.cpp-Binaries + Modell, ~7 GB – dauert eine Weile) …",
          "Setting up Bonsai (llama.cpp binaries + model, ~7 GB – takes a while) …"))
    env = {**os.environ, "BONSAI_OPENWEBUI": "0", "BONSAI_CODE_INTERPRETER": "0"}
    result = run(["sh", "./setup.sh"], cwd=str(d), env=env)
    if result.returncode != 0 or not (d / START_SCRIPT).exists():
        out(T("✘ Bonsai-Einrichtung fehlgeschlagen – Details siehe oben (FAQ: ~/bonsai/FAQ.md).",
              "✘ Bonsai setup failed – see the output above (FAQ: ~/bonsai/FAQ.md)."))
        return False
    return True


def install(state_path: Path, gpu: dict | None = None, activate: bool = True, directory: Path | None = None,
            run: Runner = subprocess.run, out=print) -> str | None:
    from .models import detect_gpu, register_profile
    d = directory or bonsai_dir()
    if not setup(d, run=run, out=out):
        return None
    profile = make_profile(gpu or detect_gpu(), d)
    return register_profile(state_path, PROFILE_NAME, profile, activate=activate)
