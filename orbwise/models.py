"""Modell-Vorauswahl: bekannte Ollama-Modelle mit Größe und Grafikspeicher-Bedarf, GPU-Erkennung und eine
interaktive Auswahl (Installer und `orbwise model add`). Zusätzlich geladene Modelle werden als Profile gemerkt
(state.json → added_models) und erscheinen im Modell-Menü der Oberfläche."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .lang import T


@dataclass(frozen=True)
class Preset:
    tag: str            # Ollama-Name
    label: str
    download_gb: float  # ungefähre Downloadgröße
    vram_gb: float      # komfortabel komplett im Grafikspeicher
    note_de: str
    note_en: str
    kind: str = "ollama"  # "ollama" oder "bonsai" (eigener llama-server aus dem PrismML-Fork)

    def note(self) -> str:
        return T(self.note_de, self.note_en)


PRESETS: list[Preset] = [
    Preset("qwen3:4b", "Qwen 3 4B", 2.6, 4, "klein und flott – für schwache GPUs oder nur CPU",
           "small and quick – for weak GPUs or CPU only"),
    Preset("qwen3:8b", "Qwen 3 8B", 5.2, 7, "gutes Tool-Calling, ideal für 8 GB",
           "good tool calling, ideal for 8 GB"),
    Preset("qwen3:14b", "Qwen 3 14B", 9.3, 12, "deutlich klüger – Empfehlung für 12–16 GB",
           "noticeably smarter – recommended for 12–16 GB"),
    Preset("qwen3:30b", "Qwen 3 30B-A3B (MoE)", 19, 20, "sehr schnell dank MoE; mit 16 GB teilweise im RAM",
           "very fast thanks to MoE; partly in RAM with 16 GB"),
    Preset("qwen3:32b", "Qwen 3 32B", 20, 24, "stark, braucht 24 GB", "strong, needs 24 GB"),
    Preset("qwen3.5:4b", "Qwen 3.5 4B", 3.4, 5, "neuere Qwen-Generation, klein", "newer Qwen generation, small"),
    Preset("qwen3.5:9b", "Qwen 3.5 9B", 6.6, 9, "neuere Qwen-Generation – für 10–16 GB",
           "newer Qwen generation – for 10–16 GB"),
    Preset("qwen3.5:27b", "Qwen 3.5 27B", 17, 22, "neuere Qwen-Generation, groß", "newer Qwen generation, large"),
    Preset("gpt-oss:20b", "gpt-oss 20B (OpenAI)", 14, 16, "starkes Tool-Calling, denkt immer ein wenig",
           "strong tool calling, always reasons a little"),
    Preset("mistral-small3.2:24b", "Mistral Small 3.2 24B", 15, 18, "gut in Deutsch/Englisch, 16 GB knapp",
           "good in German/English, 16 GB is tight"),
    Preset("llama3.1:8b", "Llama 3.1 8B", 4.9, 7, "bewährt, Tool-Calling solide", "proven, solid tool calling"),
    Preset("bonsai", "Bonsai 2 27B (llama.cpp)", 7.2, 8,
           "27B-Klasse in ~7 GB: sehr klug, denkt gründlich – eigener Server, Einrichtung automatisch",
           "27B class in ~7 GB: very smart, reasons thoroughly – own server, set up automatically", kind="bonsai"),
    Preset("bonsai-kompakt", "Bonsai 2 27B kompakt (1,75 Bit)", 5.9, 7,
           "dieselben 27B, 1,3 GB kleiner – auf 8-GB-Karten mit 16k Kontext; liest Prompts etwas langsamer ein",
           "the same 27B, 1.3 GB smaller – 16k context on 8 GB cards; reads prompts a little slower",
           kind="bonsai"),
]
BY_TAG = {p.tag: p for p in PRESETS}
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[a-z0-9._-]+)?$")


def slug(tag: str) -> str:
    """Profilname aus einem Ollama-Namen: 'qwen3:14b' → 'qwen3-14b'."""
    return re.sub(r"[^a-z0-9.-]+", "-", tag.lower()).strip("-")


def recommend(vram_gb: float) -> str:
    if vram_gb >= 22:
        return "qwen3:30b"
    if vram_gb >= 12:
        return "qwen3:14b"
    if vram_gb >= 6:
        return "qwen3:8b"
    return "qwen3:4b"


def fit(p: Preset, vram_gb: float) -> str:
    """ok = passt komplett in den Grafikspeicher, tight = teilweise im RAM (langsamer), big = zu groß."""
    if vram_gb >= p.vram_gb:
        return "ok"
    if vram_gb >= p.vram_gb * 0.7 or (vram_gb == 0 and p.vram_gb <= 5):
        return "tight"
    return "big"


# ---------------------------------------------------------------- GPU-Erkennung

def detect_gpu() -> dict:
    """{'vendor': 'nvidia'|'amd'|'none', 'name': str, 'vram_gb': float}"""
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=10).stdout.strip().splitlines()
            if out:
                name, mib = [x.strip() for x in out[0].rsplit(",", 1)]
                return {"vendor": "nvidia", "name": name, "vram_gb": round(float(mib) / 1024, 1)}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    best = 0
    for f in Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
        try:
            best = max(best, int(f.read_text().strip()))
        except (OSError, ValueError):
            pass
    if best:
        return {"vendor": "amd", "name": _amd_name(), "vram_gb": round(best / 1024 ** 3, 1)}
    return {"vendor": "none", "name": "", "vram_gb": 0.0}


def _amd_name() -> str:
    if not shutil.which("lspci"):
        return "AMD"
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return "AMD"
    for line in out.splitlines():
        if ("VGA" in line or "Display" in line) and ("AMD" in line or "ATI" in line):
            return line.split(": ", 1)[-1]
    return "AMD"


# ---------------------------------------------------------------- Ollama

def installed_models(base_url: str = "http://localhost:11434") -> set[str]:
    try:
        r = httpx.get(f"{base_url}/api/tags", timeout=5)
        return {m.get("name", "") for m in r.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return set()


def is_installed(tag: str, installed: set[str]) -> bool:
    if tag in ("bonsai", "bonsai-kompakt"):
        from .bonsai import is_set_up
        return is_set_up(variant=tag)
    return tag in installed or (":" not in tag and f"{tag}:latest" in installed)


def preset_list(vram_gb: float, installed: set[str]) -> list[dict]:
    rec = recommend(vram_gb)
    return [{**asdict(p), "note": p.note(), "fit": fit(p, vram_gb), "installed": is_installed(p.tag, installed),
             "recommended": p.tag == rec, "profile": slug(p.tag)} for p in PRESETS]


# ---------------------------------------------------------------- Zusätzliche Modelle merken (state.json)

def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def added_profiles(state_path: Path) -> dict[str, dict]:
    """Vollständige Profile (z. B. Bonsai mit eigenem Server), gespeichert von `orbwise model add`."""
    data = _read_state(state_path).get("added_profiles", {})
    return data if isinstance(data, dict) else {}


def register_profile(state_path: Path, name: str, profile: dict, activate: bool = False) -> str:
    data = _read_state(state_path)
    data.setdefault("added_profiles", {})[name] = profile
    if activate:
        data["active_profile"] = name
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return name


def added_models(state_path: Path) -> list[str]:
    return [t for t in _read_state(state_path).get("added_models", []) if isinstance(t, str) and TAG_RE.match(t)]


def register_model(state_path: Path, tag: str, activate: bool = False) -> str:
    data = _read_state(state_path)
    models = data.setdefault("added_models", [])
    if tag not in models:
        models.append(tag)
    name = slug(tag)
    if activate:
        data["active_profile"] = name
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return name


def removable(state_path: Path, name: str) -> bool:
    """Per Oberfläche/`orbwise model add` hinzugefügt (state.json) – nicht aus der config.yaml."""
    return name in added_profiles(state_path) or any(slug(t) == name for t in added_models(state_path))


def unregister_model(state_path: Path, name_or_tag: str) -> str | None:
    data = _read_state(state_path)
    profiles = data.get("added_profiles", {})
    if name_or_tag in profiles:
        profiles.pop(name_or_tag)
        if data.get("active_profile") == name_or_tag:
            data.pop("active_profile")
        state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return name_or_tag
    models = data.get("added_models", [])
    for tag in models:
        if tag == name_or_tag or slug(tag) == name_or_tag:
            models.remove(tag)
            if data.get("active_profile") == slug(tag):
                data.pop("active_profile")
            state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
            return tag
    return None


# ---------------------------------------------------------------- Interaktive Auswahl (Terminal)

FIT_MARK = {"ok": "✔", "tight": "~", "big": "✘"}


def choose_interactive(vram_gb: float, installed: set[str] | None = None, ask=input, out=print) -> str | None:
    """Nummerierte Liste; Enter = Empfehlung, Zahl = Preset, eigener Name = beliebiges Ollama-Modell."""
    installed = installed or set()
    rec = recommend(vram_gb)
    out(T(f"\nModell auswählen (Grafikspeicher: {vram_gb:g} GB)", f"\nChoose a model (video memory: {vram_gb:g} GB)"))
    out(T("  ✔ passt komplett  ~ teilweise im RAM (langsamer)  ✘ zu groß",
          "  ✔ fits completely  ~ partly in RAM (slower)  ✘ too big"))
    for i, p in enumerate(PRESETS, 1):
        tags = []
        if p.tag == rec:
            tags.append(T("EMPFOHLEN", "RECOMMENDED"))
        if is_installed(p.tag, installed):
            tags.append(T("installiert", "installed"))
        extra = f"  [{', '.join(tags)}]" if tags else ""
        out(f" {i:>2}) {FIT_MARK[fit(p, vram_gb)]} {p.label:<24} ~{p.download_gb:g} GB  {p.note()}{extra}")
    out(T("  Oder einen eigenen Ollama-Namen eingeben (z. B. qwen3:1.7b).",
          "  Or type any Ollama model name (e.g. qwen3:1.7b)."))
    while True:
        answer = ask(T(f"Auswahl [Enter = {rec}]: ", f"Choice [Enter = {rec}]: ")).strip()
        if not answer:
            return rec
        if answer.isdigit() and 1 <= int(answer) <= len(PRESETS):
            return PRESETS[int(answer) - 1].tag
        if TAG_RE.match(answer.lower()):
            return answer.lower()
        out(T("Bitte eine Nummer oder einen Modellnamen eingeben.", "Please enter a number or a model name."))
