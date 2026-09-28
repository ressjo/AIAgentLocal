"""Piper-Stimmen (Deutsch/Englisch), die sich aus der Oberfläche installieren lassen (rhasspy/piper-voices)."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


@dataclass(frozen=True)
class VoiceInfo:
    name: str          # Dateiname ohne .onnx, z. B. de_DE-thorsten-high
    label: str
    description: str
    male: bool
    speaker: str = ""  # bei Mehrsprecher-Modellen: gewünschter Sprecher

    @property
    def locale(self) -> str:
        return self.name.split("-")[0]           # z. B. de_DE, en_GB

    @property
    def language(self) -> str:
        return self.locale.split("_")[0]         # de, en

    @property
    def speaker_dir(self) -> str:
        return self.name.split("-")[1]

    @property
    def quality(self) -> str:
        return self.name.split("-")[2]

    def urls(self) -> tuple[str, str]:
        base = f"{BASE_URL}/{self.language}/{self.locale}/{self.speaker_dir}/{self.quality}/{self.name}.onnx"
        return base, base + ".json"


CATALOG: list[VoiceInfo] = [
    VoiceInfo("de_DE-thorsten-high", "Thorsten (hoch)", "Klare, neutrale Männerstimme – beste Qualität", True),
    VoiceInfo("de_DE-thorsten-medium", "Thorsten (mittel)", "Wie Thorsten, etwas schneller zu berechnen", True),
    VoiceInfo("de_DE-thorsten_emotional-medium", "Thorsten ruhig", "Thorsten mit neutral-ruhigem Sprechstil",
              True, speaker="neutral"),
    VoiceInfo("de_DE-pavoque-low", "Pavoque", "Tiefere, sonore Männerstimme – sehr butlerhaft", True),
    VoiceInfo("de_DE-karlsson-low", "Karlsson", "Markante Männerstimme", True),
    VoiceInfo("de_DE-kerstin-low", "Kerstin", "Weibliche Stimme", False),
    VoiceInfo("de_DE-ramona-low", "Ramona", "Weibliche Stimme", False),
    VoiceInfo("en_GB-alan-medium", "Alan (British)", "Calm British male voice – the butler default", True),
    VoiceInfo("en_GB-northern_english_male-medium", "Northern English", "Warm, deeper British male voice", True),
    VoiceInfo("en_US-ryan-high", "Ryan (US)", "Clear American male voice – best quality", True),
    VoiceInfo("en_US-joe-medium", "Joe (US)", "Relaxed American male voice", True),
    VoiceInfo("en_GB-jenny_dioco-medium", "Jenny (British)", "British female voice", False),
    VoiceInfo("en_US-amy-medium", "Amy (US)", "American female voice", False),
]
BY_NAME = {v.name: v for v in CATALOG}


def voice_list(voices_dir: Path, current: str, language: str = "") -> list[dict]:
    """Stimmen der gewählten Sprache (plus bereits installierte anderer Sprachen)."""
    installed = {p.stem for p in voices_dir.glob("*.onnx")} if voices_dir.exists() else set()
    out = [{**asdict(v), "installed": v.name in installed, "current": v.name == current} for v in CATALOG
           if not language or v.language == language or v.name in installed]
    # Manuell abgelegte Stimmen ebenfalls anzeigen
    for name in sorted(installed - set(BY_NAME)):
        out.append({"name": name, "label": name, "description": "Eigene Stimme", "male": True, "speaker": "",
                    "installed": True, "current": name == current})
    return out


async def install_voice(name: str, voices_dir: Path, transport: httpx.AsyncBaseTransport | None = None) -> Path:
    info = BY_NAME.get(name)
    if not info:
        raise ValueError(f"Unbekannte Stimme: {name}")
    voices_dir.mkdir(parents=True, exist_ok=True)
    target = voices_dir / f"{name}.onnx"
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=120),
                                 transport=transport) as client:
        for url, dest in zip(info.urls(), (target, voices_dir / f"{name}.onnx.json")):
            if dest.exists():
                continue
            tmp = dest.with_name(dest.name + ".part")
            log.info("Lade %s …", url)
            async with client.stream("GET", url) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"Download fehlgeschlagen ({r.status_code}): {url}")
                with tmp.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
            tmp.replace(dest)
    return target
