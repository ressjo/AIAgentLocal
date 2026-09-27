"""Root-Rechte per Passwortdialog in der Jarvis-Oberfläche (sudo -A mit eigenem Askpass-Helfer).

Ablauf: Ein Tool führt `sudo -A …` aus. sudo startet den Askpass-Helfer (kleines Skript, von Jarvis beim Start
angelegt). Der Helfer fragt per HTTP mit einem Einmal-Token beim Jarvis-Server nach, der Server zeigt in der
Oberfläche ein Passwortfeld und reicht die Eingabe an den Helfer zurück, der sie an sudo weitergibt.

Das Passwort wird nirgends gespeichert oder geloggt und erreicht nie das Sprachmodell.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import secrets
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

HELPER_TEMPLATE = '''#!{python}
# Jarvis-Askpass: wird von "sudo -A" aufgerufen und fragt das Passwort in der Jarvis-Oberfläche ab.
import json, os, sys, urllib.request
url, token = os.environ.get("JARVIS_ASKPASS_URL"), os.environ.get("JARVIS_ASKPASS_TOKEN")
if not url or not token:
    sys.exit(1)
req = urllib.request.Request(url, data=json.dumps({{"prompt": " ".join(sys.argv[1:])}}).encode(), method="POST",
                             headers={{"Content-Type": "application/json", "X-Jarvis-Askpass": token}})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))  # nie über einen Proxy
try:
    with opener.open(req, timeout=180) as resp:
        password = json.load(resp)["password"]
except Exception:
    sys.exit(1)
sys.stdout.write(password + "\\n")
'''

MAX_USES = 3        # sudo fragt bei falschem Passwort bis zu dreimal
WAIT_SECONDS = 150  # so lange wartet Jarvis auf die Eingabe

Notify = Callable[[dict], Awaitable[None]]


def helper_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/jarvis-{os.getuid()}"
    return Path(runtime) / "jarvis" / "askpass"


def write_helper(path: Path | None = None) -> Path:
    path = path or helper_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HELPER_TEMPLATE.format(python=sys.executable), encoding="utf-8")
    os.chmod(tmp, 0o700)
    tmp.replace(path)
    return path


class AskpassBroker:
    """Vergibt Einmal-Tokens pro privilegiertem Befehl und vermittelt die Passworteingabe an die Oberfläche."""

    def __init__(self, port: int, notify: Notify | None = None, helper: Path | None = None,
                 has_ui: Callable[[], bool] | None = None, say: Callable[[str], None] | None = None):
        self.url = f"http://127.0.0.1:{port}/api/askpass"
        self.notify = notify
        self.helper = helper
        self.has_ui = has_ui or (lambda: True)
        self.say = say
        self.tokens: dict[str, dict] = {}
        self.pending: dict[str, asyncio.Future] = {}

    def ready(self) -> bool:
        return bool(self.helper and self.helper.exists())

    @contextlib.contextmanager
    def grant(self, command: str):
        """Umgebung für genau einen privilegierten Befehl; das Token verfällt danach."""
        if not self.ready():
            yield {}
            return
        token = secrets.token_urlsafe(32)
        self.tokens[token] = {"uses": 0, "command": command}
        try:
            yield {"SUDO_ASKPASS": str(self.helper), "JARVIS_ASKPASS_URL": self.url, "JARVIS_ASKPASS_TOKEN": token}
        finally:
            self.tokens.pop(token, None)

    def _check(self, token: str) -> dict | None:
        for known, info in self.tokens.items():
            if hmac.compare_digest(known, token or ""):
                return info
        return None

    async def request(self, token: str, prompt: str = "") -> str | None:
        """Vom Helfer aufgerufen. None = abgelehnt/abgebrochen."""
        info = self._check(token)
        if info is None or info["uses"] >= MAX_USES or not self.notify or not self.has_ui():
            return None
        info["uses"] += 1
        req_id = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        retry = info["uses"] > 1
        await self.notify({"type": "password_request", "id": req_id, "command": info["command"], "retry": retry,
                           "prompt": "Falsches Passwort – bitte erneut eingeben" if retry else "Root-Passwort (sudo)"})
        if self.say and not retry:
            self.say("Dafür brauche ich dein Passwort. Bitte gib es im Dashboard ein.")
        try:
            return await asyncio.wait_for(fut, timeout=WAIT_SECONDS)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending.pop(req_id, None)
            await self.notify({"type": "password_done", "id": req_id})

    def answer(self, req_id: str, password: str | None) -> None:
        fut = self.pending.get(req_id)
        if fut and not fut.done():
            fut.set_result(password if password else None)

    def cancel_all(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result(None)


# Vom Server gesetzt; Tools holen sich darüber die Umgebung für sudo -A
BROKER: AskpassBroker | None = None


@contextlib.contextmanager
def privileged_env(command: str):
    if BROKER is None:
        yield {}
    else:
        with BROKER.grant(command) as env:
            yield env
