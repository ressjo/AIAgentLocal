"""Die Beispiel-Config nennt jede Option – und enthält keine echten Zugangsdaten."""

import re

import yaml
from pydantic import BaseModel

from orbwise.cli import EXAMPLE_CONFIG
from orbwise.config import Config, ProfileConfig, ServerConfig

SECRET_KEYS = ("password", "token", "api_key", "secret")


def _options(model: type[BaseModel], prefix: str = "") -> list[str]:
    out = []
    for name, field in model.model_fields.items():
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            out += _options(ann, f"{prefix}{name}.")
        else:
            out.append(prefix + name)
    return out


def test_every_option_is_documented():
    text = EXAMPLE_CONFIG.read_text()
    keys = set(re.findall(r"^[\s#]*([a-z_]+):", text, re.M))
    missing = [o for o in _options(Config) if o.rsplit(".", 1)[-1] not in keys]
    for model in (ProfileConfig, ServerConfig):  # im auskommentierten Profil-Beispiel
        missing += [f"profile.{o}" for o in _options(model) if o.rsplit(".", 1)[-1] not in keys]
    assert not missing, f"in config.example.yaml fehlen: {missing}"


def test_example_has_no_credentials_and_loads():
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    Config.model_validate(data)

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif any(s in path.rsplit(".", 1)[-1] for s in SECRET_KEYS):
            assert node in ("", None, 0), f"{path} enthält einen Wert"

    walk(data)
    assert not re.search(r"(password|token|api_key)\S*:\s*\"[^\"<]+\"", EXAMPLE_CONFIG.read_text())
