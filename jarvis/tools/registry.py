"""Tool-Registry: Funktionen werden per Decorator registriert, das JSON-Schema für Ollama
wird aus den Typannotationen (Annotated[typ, "Beschreibung"]) erzeugt."""

from __future__ import annotations

import inspect
import json
import logging
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, get_args, get_origin

log = logging.getLogger(__name__)

SAFE, CONFIRM, BLOCKED = "safe", "confirm", "blocked"

RiskFn = Callable[["ToolContext", dict], tuple[str, str]]


@dataclass
class ToolContext:
    cfg: Any
    memory: Any
    emit: Callable[[dict], Awaitable[None]] | None = None
    call_id: str = ""
    # Gemeinsame Dienste des Servers (z. B. "reminders": ReminderStore)
    services: dict = field(default_factory=dict)

    async def output(self, text: str) -> None:
        """Live-Ausgabe eines laufenden Tools an die Oberfläche."""
        if self.emit:
            await self.emit({"type": "tool_output", "id": self.call_id, "text": text})


@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable[..., Awaitable[str]]
    parameters: dict
    risk: str | RiskFn = SAFE
    param_types: dict[str, type] = field(default_factory=dict)
    enabled: Callable[[Any], bool] | None = None
    group: str = ""  # Modulname, z. B. "sysadmin" – ganze Gruppen lassen sich per tools.disabled abschalten

    def is_enabled(self, cfg: Any) -> bool:
        if cfg is None:
            return True
        disabled = set(getattr(getattr(cfg, "tools", None), "disabled", None) or [])
        if self.name in disabled or self.group in disabled:
            return False
        return self.enabled is None or bool(self.enabled(cfg))

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def assess(self, ctx: ToolContext, args: dict) -> tuple[str, str]:
        if callable(self.risk):
            return self.risk(ctx, args)
        return self.risk, ""


REGISTRY: dict[str, ToolSpec] = {}

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _json_type(tp: Any) -> tuple[str, type]:
    origin = get_origin(tp)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        inner = [a for a in get_args(tp) if a is not type(None)]
        return _json_type(inner[0])
    if origin is list:
        return "array", list
    return _JSON_TYPES.get(tp, "string"), tp if tp in _JSON_TYPES else str


def tool(description: str, risk: str | RiskFn = SAFE, name: str | None = None,
         enabled: Callable[[Any], bool] | None = None):
    def deco(func):
        sig = inspect.signature(func)
        hints = typing.get_type_hints(func, include_extras=True)
        props, required, types = {}, [], {}
        for pname, param in list(sig.parameters.items())[1:]:  # erstes Argument ist ctx
            hint = hints.get(pname, str)
            desc = ""
            if get_origin(hint) is Annotated:
                hint, desc = get_args(hint)[0], get_args(hint)[1]
            jtype, pytype = _json_type(hint)
            prop: dict[str, Any] = {"type": jtype}
            if jtype == "array":
                prop["items"] = {"type": "string"}
            if desc:
                prop["description"] = desc
            props[pname] = prop
            types[pname] = pytype
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        spec = ToolSpec(
            name=name or func.__name__,
            description=description,
            func=func,
            parameters={"type": "object", "properties": props, "required": required},
            risk=risk,
            param_types=types,
            enabled=enabled,
            group=func.__module__.rsplit(".", 1)[-1],
        )
        REGISTRY[spec.name] = spec
        return func
    return deco


def coerce_args(spec: ToolSpec, raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    args = {}
    for key, value in raw.items():
        tp = spec.param_types.get(key)
        if tp is None:
            continue  # unbekannte Parameter ignorieren (LLM halluziniert gelegentlich)
        try:
            if tp is bool and isinstance(value, str):
                value = value.strip().lower() in ("true", "1", "ja", "yes")
            elif tp in (int, float) and not isinstance(value, bool):
                value = tp(value)
            elif tp is str and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        except (TypeError, ValueError):
            continue
        args[key] = value
    return args


def missing_args(spec: ToolSpec, args: dict) -> list[str]:
    return [p for p in spec.parameters["required"] if p not in args]


def tool_schemas(cfg: Any = None) -> list[dict]:
    """Schemas aller Tools; mit cfg nur die aktivierten (z. B. Trilium nur, wenn konfiguriert)."""
    return [s.schema() for s in REGISTRY.values() if s.is_enabled(cfg)]


def get_tool(name: str) -> ToolSpec | None:
    return REGISTRY.get(name)


def load_all_tools() -> dict[str, ToolSpec]:
    # Import registriert die Tools per Decorator
    from . import (  # noqa: F401
        apps,
        briefing,
        calendar_tools,
        files,
        homeassistant,
        memory_tools,
        packages,
        paperless,
        power,
        reminder_tools,
        shell,
        sysadmin,
        system,
        trilium,
        weather,
        web,
    )
    return REGISTRY
