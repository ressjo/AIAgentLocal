"""Konfiguration: Standardwerte + ~/.config/orbwise/config.yaml (oder $ORBWISE_CONFIG)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "orbwise"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "orbwise"


def env(name: str, default: str = "") -> str:
    """$ORBWISE_<name> – oder der frühere Name $JARVIS_<name> (vor der Umbenennung hieß das Projekt „Jarvis“)."""
    return os.environ.get(f"ORBWISE_{name}") or os.environ.get(f"JARVIS_{name}") or default


class ServerConfig(BaseModel):
    """Optionaler Modell-Server, den Orbwise selbst startet und stoppt (z. B. llama-server für Bonsai)."""
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


DEFAULT_VOICES = {"de": "de_DE-thorsten-high", "en": "en_GB-alan-medium"}


class VoiceConfig(BaseModel):
    enabled: bool = True
    # Sprache der Spracherkennung – leer = wie die globale Einstellung "language"
    language: str = ""
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    # Piper-Stimme – leer = Standardstimme der gewählten Sprache
    tts_voice: Path | None = None
    tts_length_scale: float = 0.95
    wakeword_model: str = "hey_jarvis"
    wakeword_threshold: float = 0.5
    silence_ms: int = 900
    max_record_seconds: int = 20
    no_speech_timeout_seconds: float = 5.0


class ToolsConfig(BaseModel):
    search_paths: list[Path] = Field(default_factory=lambda: [Path.home()])
    nas_paths: list[Path] = Field(default_factory=list)
    # "dashboard" (Passwortfeld in der Orbwise-Oberfläche, sudo -A), "pkexec" (Polkit-Dialog des Systems)
    # oder "sudo" (sudo -n, benötigt NOPASSWD-Regel)
    privilege_cmd: str = "dashboard"
    # "auto" (erkennen), "pacman" (Arch/Manjaro/EndeavourOS) oder "apt" (Debian/Ubuntu/Mint)
    package_manager: str = "auto"
    # "auto" (yay/paru suchen), "yay", "paru" oder "none"
    aur_helper: str = "auto"
    shell_timeout: int = 120
    # Tools oder ganze Gruppen abschalten (spart Kontext bei kleinen Modellen), z. B. [sysadmin, web, open_ports]
    disabled: list[str] = Field(default_factory=list)
    # so viele Modellschritte (Tool-Runden) darf eine Aufgabe höchstens brauchen
    max_steps: int = 25
    update_timeout: int = 3600
    max_output_chars: int = 6000
    searxng_url: str | None = None
    # Brave Search API (offizielle Schnittstelle, kein Auslesen von Suchseiten); alternativ $ORBWISE_BRAVE_API_KEY
    brave_api_key: str = ""
    # mit Brave-Schlüssel bei Fehlern trotzdem auf das Auslesen der Suchseiten (ddgs) ausweichen?
    search_fallback: bool = False

    @property
    def brave_key(self) -> str:
        return self.brave_api_key or env("BRAVE_API_KEY")

    @field_validator("privilege_cmd")
    @classmethod
    def _privilege(cls, v: str) -> str:
        # frühere Namen des Passwortfelds (vor der Umbenennung hieß das Projekt „Jarvis“)
        return "dashboard" if v in ("jarvis", "orbwise") else v

    @field_validator("search_paths", "nas_paths", mode="before")
    @classmethod
    def _paths(cls, v):
        # In YAML wird ein nacktes ~ zu None – als Home-Verzeichnis interpretieren
        if v is None:
            return []
        return ["~" if p is None else p for p in v]


class ObsidianConfig(BaseModel):
    # Pfad zum Obsidian-Vault (normaler Ordner mit Markdown-Dateien), z. B. ~/Obsidian/Notes
    vault: str = ""
    # Ordner im Vault für neue Notizen
    inbox: str = "Inbox"
    # so viel Notiztext geht höchstens an das Modell (größere Notizen: abschnittsweise bzw. nur relevante Stellen)
    max_chars: int = 8000

    @property
    def path(self) -> Path | None:
        return Path(self.vault).expanduser() if self.vault.strip() else None

    @property
    def enabled(self) -> bool:
        return bool(self.path and self.path.is_dir())


class TriliumConfig(BaseModel):
    # z. B. http://localhost:8080 oder die Adresse auf dem NAS
    url: str = ""
    # ETAPI-Token (Trilium: Optionen → ETAPI); alternativ $ORBWISE_TRILIUM_TOKEN
    token: str = ""
    timeout: float = 20.0
    max_chars: int = 8000
    # HTTPS mit selbstsigniertem Zertifikat: false – oder Pfad zur CA-/Zertifikatsdatei
    verify_ssl: bool | str = True

    @property
    def api_token(self) -> str:
        return self.token or env("TRILIUM_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class PaperlessConfig(BaseModel):
    # z. B. http://nas.local:8000
    url: str = ""
    # API-Token (Paperless → Profil oben rechts → API-Auth-Token); alternativ $ORBWISE_PAPERLESS_TOKEN
    token: str = ""
    timeout: float = 30.0
    # HTTPS mit selbstsigniertem Zertifikat: false – oder Pfad zur CA-/Zertifikatsdatei
    verify_ssl: bool | str = True
    # so viel Dokumenttext geht höchstens an das Modell (größere Dokumente: nur relevante Stellen)
    max_chars: int = 5000

    @property
    def api_token(self) -> str:
        return self.token or env("PAPERLESS_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class HomeAssistantConfig(BaseModel):
    # z. B. http://homeassistant.local:8123
    url: str = ""
    # Langlebiges Zugriffstoken (Profil → Sicherheit); alternativ $ORBWISE_HA_TOKEN
    token: str = ""
    timeout: float = 15.0
    verify_ssl: bool | str = True

    @property
    def api_token(self) -> str:
        return self.token or env("HA_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.api_token)


class CalendarConfig(BaseModel):
    # iCloud: https://caldav.icloud.com – funktioniert mit jedem CalDAV-Server (Nextcloud, Radicale, …)
    url: str = ""
    username: str = ""
    # App-spezifisches Passwort (Apple-ID → Anmeldung und Sicherheit); alternativ $ORBWISE_CALENDAR_PASSWORD
    password: str = ""
    # Kalender, die gelesen werden (leer = alle)
    calendars: list[str] = Field(default_factory=list)
    # Kalender für neue Termine (leer = erster gelesener)
    default_calendar: str = ""
    timeout: float = 20.0

    @property
    def api_password(self) -> str:
        return self.password or env("CALENDAR_PASSWORD")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.username and self.api_password)


class WeatherConfig(BaseModel):
    # Standardort, z. B. "Freiburg im Breisgau" – leer = Orbwise fragt nach dem Ort
    location: str = ""


class Config(BaseModel):
    # Sprache von Orbwise und der Oberfläche: "de" (Deutsch) oder "en" (English)
    language: str = "de"
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
    obsidian: ObsidianConfig = Field(default_factory=ObsidianConfig)
    paperless: PaperlessConfig = Field(default_factory=PaperlessConfig)
    homeassistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    # Zusätzliche Websites für open_website: Name → URL; "{q}" wird durch die Suche ersetzt
    websites: dict[str, str] = Field(default_factory=dict)

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str) -> str:
        v = (v or "de").strip().lower()[:2]
        return v if v in ("de", "en") else "de"

    @model_validator(mode="after")
    def _language_defaults(self) -> "Config":
        if not self.voice.language:
            self.voice.language = self.language
        if self.voice.tts_voice is None:
            self.voice.tts_voice = DATA_DIR / "voices" / f"{DEFAULT_VOICES[self.language]}.onnx"
        return self

    def expand_paths(self) -> "Config":
        self.memory.dir = self.memory.dir.expanduser()
        self.voice.tts_voice = self.voice.tts_voice.expanduser()
        self.tools.search_paths = [p.expanduser() for p in self.tools.search_paths]
        self.tools.nas_paths = [p.expanduser() for p in self.tools.nas_paths]
        return self


def config_path() -> Path:
    return Path(env("CONFIG") or CONFIG_DIR / "config.yaml").expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data).expand_paths()
