"""Piper-Stimmen (Deutsch/Englisch), die sich aus der Oberfläche installieren lassen (rhasspy/piper-voices)."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import time
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


# Der vollständige Piper-Katalog (alle Sprachen, ~200 Stimmen) – wird täglich einmal geladen und zwischengespeichert
CATALOG_URL = f"{BASE_URL}/voices.json"
CATALOG_FILE = "voices_catalog.json"
CATALOG_MAX_AGE = 24 * 3600
RETRY_AFTER = 600  # nach einem Fehlschlag 10 min nicht erneut versuchen
_last_failure = 0.0
NAME_RE = re.compile(r"^[a-z]{2,3}_[A-Z]{2}-[a-z0-9_]+-(x_low|low|medium|high)$")


async def fetch_catalog(cache_dir: Path, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """voices.json von Hugging Face (Cache 24 h). Offline: letzte Kopie, sonst {} – dann gilt nur CATALOG."""
    global _last_failure
    cache = cache_dir / CATALOG_FILE
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < CATALOG_MAX_AGE
    if fresh or time.time() - _last_failure < RETRY_AFTER:  # offline: nicht bei jedem Öffnen des Menüs warten
        with contextlib.suppress(ValueError, OSError):
            return json.loads(cache.read_text(encoding="utf-8"))
        if not fresh:
            return {}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15, transport=transport) as client:
            r = await client.get(CATALOG_URL)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, dict):
            raise ValueError("unerwartetes Format")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
        return data
    except (httpx.HTTPError, ValueError, OSError) as e:
        _last_failure = time.time()
        log.info("Piper-Katalog nicht ladbar (%s) – nutze %s", e, "Cache" if cache.exists() else "nur die Auswahl")
        with contextlib.suppress(ValueError, OSError):
            return json.loads(cache.read_text(encoding="utf-8"))
        return {}


def _extra_entry(name: str, info: dict, en: bool) -> dict:
    """Katalogeintrag ohne eigene Beschreibung: Label aus dem Sprechernamen, Rest aus den Metadaten."""
    locale = (info.get("language") or {}).get("code") or name.split("-")[0]
    speaker = name.split("-")[1] if name.count("-") >= 2 else name
    quality = info.get("quality") or name.rsplit("-", 1)[-1]
    speakers = int(info.get("num_speakers") or 1)
    size = sum(int(f.get("size_bytes") or 0) for f in (info.get("files") or {}).values())
    desc = f"{locale} · {'quality' if en else 'Qualität'} {quality}"
    if speakers > 1:
        desc += f" · {speakers} {'speakers' if en else 'Sprecher'}"
    return {"name": name, "label": speaker.replace("_", " ").title(), "description": desc, "male": None,
            "speaker": "", "locale": locale, "download_mb": round(size / 1e6) if size else None}


def voice_list(voices_dir: Path, current: str, language: str = "", extra: dict | None = None) -> list[dict]:
    """Stimmen der gewählten Sprache (plus bereits installierte anderer Sprachen): erst die Auswahl (empfohlen),
    dann alle weiteren aus dem Piper-Katalog (extra = voices.json), dann selbst abgelegte."""
    installed = {p.stem for p in voices_dir.glob("*.onnx")} if voices_dir.exists() else set()
    extra = extra or {}
    en = language == "en"
    out = []
    for v in CATALOG:
        if not language or v.language == language or v.name in installed:
            size = sum(int(f.get("size_bytes") or 0) for f in (extra.get(v.name, {}).get("files") or {}).values())
            out.append({**asdict(v), "locale": v.locale, "recommended": True,
                        "download_mb": round(size / 1e6) if size else None})
    for name in sorted(extra):
        if name in BY_NAME or not NAME_RE.match(name):
            continue
        if language and not name.startswith(language + "_") and name not in installed:
            continue
        out.append({**_extra_entry(name, extra[name], en), "recommended": False})
    known = {v["name"] for v in out}
    # Manuell abgelegte Stimmen ebenfalls anzeigen
    for name in sorted(installed - known):
        out.append({"name": name, "label": name, "description": "Own voice" if en else "Eigene Stimme", "male": None,
                    "speaker": "", "locale": name.split("-")[0], "recommended": False, "download_mb": None})
    for v in out:
        v["installed"] = v["name"] in installed
        v["current"] = v["name"] == current
    return out


def installable(name: str, extra: dict | None = None) -> bool:
    """Nur Stimmen aus der Auswahl oder dem offiziellen Katalog – kein beliebiger Download."""
    return name in BY_NAME or (bool(NAME_RE.match(name)) and name in (extra or {}))


def urls_for(name: str) -> tuple[str, str]:
    lang, locale = name.split("_")[0], name.split("-")[0]
    _, speaker, quality = name.split("-")
    base = f"{BASE_URL}/{lang}/{locale}/{speaker}/{quality}/{name}.onnx"
    return base, base + ".json"


async def install_voice(name: str, voices_dir: Path, transport: httpx.AsyncBaseTransport | None = None,
                        extra: dict | None = None) -> Path:
    if not installable(name, extra):
        raise ValueError(f"Unbekannte Stimme: {name}")
    info = BY_NAME.get(name)
    voices_dir.mkdir(parents=True, exist_ok=True)
    target = voices_dir / f"{name}.onnx"
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=120),
                                 transport=transport) as client:
        for url, dest in zip(info.urls() if info else urls_for(name), (target, voices_dir / f"{name}.onnx.json")):
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
