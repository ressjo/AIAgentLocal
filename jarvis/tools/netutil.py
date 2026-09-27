"""Gemeinsame Helfer für Dienste im Heimnetz (Trilium, Paperless): URL bereinigen, Zertifikate, klare Fehlertexte."""

from __future__ import annotations

import os
import re
import ssl
from pathlib import Path

import httpx

TLS_HINT = ("Selbstsigniertes Zertifikat? In der Config beim Dienst `verify_ssl: false` setzen "
            "(oder den Pfad zur CA-Datei angeben).")


def normalize_url(url: str, strip_suffix: str = "") -> str:
    """'https:///host:8444/api/' → 'https://host:8444' (Tippfehler, fehlendes Schema, API-Suffix)."""
    url = (url or "").strip()
    if not url:
        return ""
    url = re.sub(r"^(https?):/*", r"\1://", url, flags=re.I)
    if not re.match(r"^https?://", url, flags=re.I):
        url = "http://" + url.lstrip("/")
    url = url.rstrip("/")
    if strip_suffix and url.lower().endswith(strip_suffix):
        url = url[: -len(strip_suffix)].rstrip("/")
    return url


def verify_arg(value: bool | str) -> bool | ssl.SSLContext:
    """verify_ssl aus der Config: true/false oder Pfad zu einer CA-/Zertifikatsdatei."""
    if isinstance(value, str) and value.strip():
        path = Path(os.path.expanduser(value.strip()))
        return ssl.create_default_context(cafile=str(path))
    return bool(value)


def client_kwargs(verify: bool | str, timeout: float) -> dict:
    return {
        "verify": verify_arg(verify),
        "timeout": timeout,
        # Heimnetz-Dienste nie über einen System-Proxy (http_proxy/https_proxy) ansprechen
        "trust_env": False,
        "follow_redirects": True,
    }


def explain(e: Exception, service: str, url: str) -> str:
    """Verständliche Fehlermeldung mit Adresse, Grund und Tipp."""
    text = f"{e!s} {e.__cause__!s} {e.__context__!s}".lower()
    where = f"{service} unter {url}"
    if isinstance(e, httpx.TimeoutException):
        reason = "antwortet nicht rechtzeitig (Zeitüberschreitung)"
        tip = "Server überlastet oder Adresse/Port falsch?"
    elif "certificate" in text or "ssl" in text or "tls" in text or "wrong version number" in text:
        if "wrong version number" in text or "record layer" in text:
            reason = "spricht kein HTTPS"
            tip = "Adresse mit http:// statt https:// versuchen."
        else:
            reason = "hat ein Zertifikat, dem nicht vertraut wird"
            tip = TLS_HINT
    elif "name or service not known" in text or "nodename nor servname" in text or "getaddrinfo" in text \
            or "name resolution" in text:
        reason = "– Hostname nicht auflösbar"
        tip = "Tippfehler in der Adresse? Sonst die IP-Adresse eintragen."
    elif isinstance(e, (httpx.ReadError, httpx.RemoteProtocolError)) and url.lower().startswith("http://"):
        reason = "bricht die Verbindung ab"
        tip = "Spricht der Dienst HTTPS? Dann die Adresse mit https:// eintragen."
    elif "refused" in text or "all connection attempts failed" in text:
        reason = "lehnt die Verbindung ab"
        tip = "Läuft der Dienst, und stimmt der Port?"
    elif "unreachable" in text or "no route" in text:
        reason = "ist im Netz nicht erreichbar"
        tip = "Gerät an? Gleiches Netz/VPN?"
    else:
        reason = f"ist nicht erreichbar ({e.__class__.__name__}: {str(e)[:120] or 'ohne Details'})"
        tip = "Adresse, Port und http/https prüfen."
    return f"{where} {reason}. {tip} Prüfen mit `jarvis doctor` – keine weitere Diagnose per Shell nötig."


def html_instead_of_json(r: httpx.Response) -> bool:
    return "text/html" in r.headers.get("content-type", "")
