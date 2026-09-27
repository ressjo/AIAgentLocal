"""Konfiguration: Standardwerte + ~/.config/jarvis/config.yaml (oder $JARVIS_CONFIG)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "jarvis"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "jarvis"


class ServerConfig(BaseModel):
    """Optionaler Modell-Server, den Jarvis selbst startet und stoppt (z. B. llama-server für Bonsai)."""
    command: str
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    startup_timeout: float = 240.0
    # Standard: <base_url ohne /v1>/health
    health_url: str = ""


class ProfileConfig(BaseModel):
    """Ein Modell-Profil. Leere Felder übernehmen die Werte aus dem llm-Block."""
    label: str = ""
    # "ollama" oder "openai" (OpenAI-kompatibler Server: llama-server, LM Studio, vLLM …)
    backend: str = "ollama"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float | None = None
    num_ctx: int | None = None
    think: bool | None = None
    # Embeddings (Gedächtnis) über Ollama auf der CPU rechnen – spart Grafikspeicher
    embed_on_cpu: bool = False
    # Vor dem Aktivieren alle Ollama-Modelle aus dem Grafikspeicher entladen
    unload_ollama: bool = False
    server: ServerConfig | None = None

    @field_validator("backend")
    @classmethod
    def _backend(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("ollama", "openai"):
            raise ValueError("backend muss 'ollama' oder 'openai' sein")
        return v


class LLMConfig(BaseModel):
    # Adresse von Ollama (für Ollama-Profile und immer für die Embeddings des Gedächtnisses)
    base_url: str = "http://localhost:11434"
    model: str = "qwen3:14b"
    embed_model: str = "bge-m3"
    temperature: float = 0.6
    num_ctx: int = 16384
    # Qwen3 "Thinking" abschalten – deutlich schnellere Antworten
    think: bool = False
    keep_alive: str = "30m"
    request_timeout: float = 300.0
    # Modell-Profile; leer = ein Profil "standard" aus den Werten oben
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    # Profil beim Start (eine Auswahl in der Oberfläche wird gemerkt und hat Vorrang)
    active: str = ""

    def resolved_profiles(self) -> dict[str, ProfileConfig]:
        """Alle Profile mit aufgefüllten Standardwerten."""
        raw = self.profiles or {"standard": ProfileConfig(backend="ollama")}
        out = {}
        for name, p in raw.items():
            default_url = self.base_url if p.backend == "ollama" else "http://127.0.0.1:8080/v1"
            out[name] = p.model_copy(update={
                "label": p.label or name,
                "base_url": (p.base_url or default_url).rstrip("/"),
                "model": p.model or (self.model if p.backend == "ollama" else "default"),
                "temperature": self.temperature if p.temperature is None else p.temperature,
                "num_ctx": self.num_ctx if p.num_ctx is None else p.num_ctx,
                "think": self.think if p.think is None else p.think,
            })
        return out


class MemoryConfig(BaseModel):
    dir: Path = DATA_DIR / "memory"
    # Gesamtbudget (geschätzte Tokens) für den Prompt; muss deutlich unter num_ctx liegen
    context_budget_tokens: int | None = None  # None = automatisch: Kontextfenster des Modells minus Antwortreserve
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
    # so viele Modellschritte (Tool-Runden) darf eine Aufgabe höchstens brauchen
    max_steps: int = 25
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
    # HTTPS mit selbstsigniertem Zertifikat: false – oder Pfad zur CA-/Zertifikatsdatei
    verify_ssl: bool | str = True

    @property
    def api_token(self) -> str:
        return self.token or os.environ.get("JARVIS_TRILIUM_TOKEN", "")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class PaperlessConfig(BaseModel):
    # z. B. http://nas.local:8000
    url: str = ""
    # API-Token (Paperless → Profil oben rechts → API-Auth-Token); alternativ $JARVIS_PAPERLESS_TOKEN
    token: str = ""
    timeout: float = 30.0
    # HTTPS mit selbstsigniertem Zertifikat: false – oder Pfad zur CA-/Zertifikatsdatei
    verify_ssl: bool | str = True
    # so viel Dokumenttext geht höchstens an das Modell (größere Dokumente: nur relevante Stellen)
    max_chars: int = 5000

    @property
    def api_token(self) -> str:
        return self.token or os.environ.get("JARVIS_PAPERLESS_TOKEN", "")

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
    paperless: PaperlessConfig = Field(default_factory=PaperlessConfig)
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
