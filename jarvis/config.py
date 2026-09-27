"""Konfiguration: Standardwerte + ~/.config/jarvis/config.yaml (oder $JARVIS_CONFIG)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "jarvis"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "jarvis"


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:11434"
    model: str = "qwen3:14b"
    embed_model: str = "bge-m3"
    temperature: float = 0.6
    num_ctx: int = 16384
    # Qwen3 "Thinking" abschalten – deutlich schnellere Antworten
    think: bool = False
    keep_alive: str = "30m"
    request_timeout: float = 300.0


class MemoryConfig(BaseModel):
    dir: Path = DATA_DIR / "memory"
    # Gesamtbudget (geschätzte Tokens) für den Prompt; muss deutlich unter num_ctx liegen
    context_budget_tokens: int = 11000
    retrieval_top_k: int = 6
    retrieval_max_tokens: int = 1500
    facts_max_tokens: int = 1200
    # Tagesübersicht erzeugen, wenn so viele Minuten keine Aktivität war
    summarize_idle_minutes: int = 15


class VoiceConfig(BaseModel):
    enabled: bool = True
    language: str = "de"
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    tts_voice: Path = DATA_DIR / "voices" / "de_DE-thorsten-high.onnx"
    tts_length_scale: float = 0.95
    wakeword_model: str = "hey_jarvis"
    wakeword_threshold: float = 0.5
    silence_ms: int = 900
    max_record_seconds: int = 20
    no_speech_timeout_seconds: float = 5.0


class ToolsConfig(BaseModel):
    search_paths: list[Path] = Field(default_factory=lambda: [Path.home()])
    nas_paths: list[Path] = Field(default_factory=list)
    # "pkexec" (grafischer Polkit-Dialog) oder "sudo" (sudo -n, benötigt NOPASSWD-Regel)
    privilege_cmd: str = "pkexec"
    # "auto" (yay/paru suchen), "yay", "paru" oder "none"
    aur_helper: str = "auto"
    shell_timeout: int = 120
    update_timeout: int = 3600
    max_output_chars: int = 6000
    searxng_url: str | None = None

    @field_validator("search_paths", "nas_paths", mode="before")
    @classmethod
    def _paths(cls, v):
        # In YAML wird ein nacktes ~ zu None – als Home-Verzeichnis interpretieren
        if v is None:
            return []
        return ["~" if p is None else p for p in v]


class TriliumConfig(BaseModel):
    # z. B. http://localhost:8080 oder die Adresse auf dem NAS
    url: str = ""
    # ETAPI-Token (Trilium: Optionen → ETAPI); alternativ $JARVIS_TRILIUM_TOKEN
    token: str = ""
    timeout: float = 20.0
    max_chars: int = 8000

    @property
    def api_token(self) -> str:
        return self.token or os.environ.get("JARVIS_TRILIUM_TOKEN", "")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class CalendarConfig(BaseModel):
    # iCloud: https://caldav.icloud.com – funktioniert mit jedem CalDAV-Server (Nextcloud, Radicale, …)
    url: str = ""
    username: str = ""
    # App-spezifisches Passwort (Apple-ID → Anmeldung und Sicherheit); alternativ $JARVIS_CALENDAR_PASSWORD
    password: str = ""
    # Kalender, die gelesen werden (leer = alle)
    calendars: list[str] = Field(default_factory=list)
    # Kalender für neue Termine (leer = erster gelesener)
    default_calendar: str = ""
    timeout: float = 20.0

    @property
    def api_password(self) -> str:
        return self.password or os.environ.get("JARVIS_CALENDAR_PASSWORD", "")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.username and self.api_password)


class WeatherConfig(BaseModel):
    # Standardort, z. B. "Freiburg im Breisgau" – leer = Jarvis fragt nach dem Ort
    location: str = ""


class Config(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    assistant_name: str = "Jarvis"
    user_name: str = ""
    # Zusätzliche Persönlichkeits-/Verhaltensanweisungen für den System-Prompt
    persona_extra: str = ""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    trilium: TriliumConfig = Field(default_factory=TriliumConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    # Zusätzliche Websites für open_website: Name → URL; "{q}" wird durch die Suche ersetzt
    websites: dict[str, str] = Field(default_factory=dict)

    def expand_paths(self) -> "Config":
        self.memory.dir = self.memory.dir.expanduser()
        self.voice.tts_voice = self.voice.tts_voice.expanduser()
        self.tools.search_paths = [p.expanduser() for p in self.tools.search_paths]
        self.tools.nas_paths = [p.expanduser() for p in self.tools.nas_paths]
        return self


def config_path() -> Path:
    return Path(os.environ.get("JARVIS_CONFIG", CONFIG_DIR / "config.yaml")).expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data).expand_paths()
