"""Paperless-ngx über die REST-API: Dokumente suchen, befragen, lesen und lokal als PDF öffnen (nur lesend).

Einrichtung: in Paperless oben rechts auf das Profil → „API-Auth-Token“ erzeugen und in der Config eintragen:
    paperless:
      url: http://nas.local:8000
      token: "…"
"""

from __future__ import annotations

import html
import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Any

import httpx

from . import proc
from .netutil import client_kwargs, explain, html_instead_of_json, normalize_url
from .registry import ToolContext, tool

# Für Tests austauschbar (httpx.MockTransport)
TRANSPORT: httpx.AsyncBaseTransport | None = None



class PaperlessError(RuntimeError):
    pass


def _enabled(cfg: Any) -> bool:
    return cfg.paperless.enabled


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "jarvis" / "paperless"


class PaperlessClient:
    def __init__(self, cfg):
        p = cfg.paperless
        self.max_chars = p.max_chars
        self.url = normalize_url(p.url, "/api")
        self.client = httpx.AsyncClient(
            base_url=self.url + "/api",
            headers={"Authorization": f"Token {p.api_token}", "Accept": "application/json"},
            transport=TRANSPORT,
            **client_kwargs(p.verify_ssl, p.timeout),
        )
        self._names: dict[str, dict[int, str]] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def request(self, method: str, path: str, **kw) -> httpx.Response:
        for attempt in (1, 2):
            try:
                r = await self.client.request(method, path, **kw)
                break
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if attempt == 2 or "certificate" in str(e).lower() or "name or service" in str(e).lower():
                    raise PaperlessError(explain(e, "Paperless", self.url)) from e
            except httpx.HTTPError as e:
                raise PaperlessError(explain(e, "Paperless", self.url)) from e
        if r.status_code in (401, 403):
            raise PaperlessError("Der Paperless-Token ist ungültig oder hat keine Rechte "
                                 "(Paperless → Profil → API-Auth-Token).")
        return r

    async def json(self, path: str, **params) -> Any:
        r = await self.request("GET", path, params=params)
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise PaperlessError(f"Paperless-Fehler {r.status_code}: {r.text[:200]}")
        if html_instead_of_json(r):
            raise PaperlessError(f"Unter {self.url} antwortet eine Webseite statt der Paperless-API – "
                                 "Adresse prüfen (nur Basis-URL, z. B. https://nas:8444).")
        return r.json()

    async def names(self, kind: str) -> dict[int, str]:
        """ID → Name für correspondents, tags, document_types (einmal pro Client geladen)."""
        if kind not in self._names:
            data = await self.json(f"/{kind}/", page_size=1000) or {}
            self._names[kind] = {x["id"]: x.get("name", "") for x in data.get("results", [])}
        return self._names[kind]

    async def document(self, doc_id: int) -> dict:
        doc = await self.json(f"/documents/{int(doc_id)}/")
        if not doc:
            raise PaperlessError(f"Kein Dokument mit der ID {doc_id} gefunden.")
        return doc

    async def describe(self, doc: dict) -> str:
        corr = (await self.names("correspondents")).get(doc.get("correspondent"), "")
        dtype = (await self.names("document_types")).get(doc.get("document_type"), "")
        tags = await self.names("tags")
        tag_names = [tags.get(t, str(t)) for t in doc.get("tags") or []]
        parts = [f"[{doc['id']}] {doc.get('title') or '(ohne Titel)'}", str(doc.get("created") or "")[:10]]
        if corr:
            parts.append(f"von {corr}")
        if dtype:
            parts.append(dtype)
        if tag_names:
            parts.append("Tags: " + ", ".join(tag_names))
        return " · ".join(p for p in parts if p)


async def _guard(coro) -> str:
    try:
        return await coro
    except PaperlessError as e:
        return str(e)


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


# ---------------------------------------------------------------- Passagen auswählen („Dokument befragen“)
# gemeinsame Logik mit Obsidian: passages.py
from .passages import keyword_scores, rank_passages, split_passages  # noqa: E402,F401


def _embedder(ctx: ToolContext):
    llm = getattr(ctx.memory, "llm", None)
    return getattr(llm, "embed", None)


# ---------------------------------------------------------------- Tools

@tool("Durchsucht die Dokumente in Paperless (Volltext inkl. OCR-Text von Rechnungen, Verträgen, Briefen, Bescheiden). "
      "Ohne Suchbegriff: die neuesten Dokumente. Liefert IDs für paperless_ask / paperless_open.", enabled=_enabled)
async def paperless_search(
    ctx: ToolContext,
    query: Annotated[str, "Suchbegriffe, z. B. 'Handyvertrag Kündigung' (leer = neueste Dokumente)"] = "",
    correspondent: Annotated[str, "Optional: Absender/Korrespondent, z. B. 'Telekom'"] = "",
    tag: Annotated[str, "Optional: Tag, z. B. 'Steuer'"] = "",
    document_type: Annotated[str, "Optional: Dokumenttyp, z. B. 'Rechnung'"] = "",
    date_from: Annotated[str, "Optional: ab Datum YYYY-MM-DD"] = "",
    date_to: Annotated[str, "Optional: bis Datum YYYY-MM-DD"] = "",
    limit: Annotated[int, "Maximale Anzahl Treffer"] = 8,
) -> str:
    async def run() -> str:
        params: dict[str, Any] = {"page_size": max(1, min(int(limit), 25)), "truncate_content": "true"}
        if query.strip():
            params["query"] = query.strip()
        else:
            params["ordering"] = "-created"
        if correspondent.strip():
            params["correspondent__name__icontains"] = correspondent.strip()
        if tag.strip():
            params["tags__name__iexact"] = tag.strip()
        if document_type.strip():
            params["document_type__name__icontains"] = document_type.strip()
        for key, val in (("created__gte", date_from), ("created__lte", date_to)):
            if val.strip():
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val.strip()):
                    return f"Datum bitte als YYYY-MM-DD angeben (nicht '{val}')."
                params[key] = val.strip()
        async with PaperlessClient(ctx.cfg) as pc:
            data = await pc.json("/documents/", **params) or {}
            docs = data.get("results", [])
            if not docs:
                return "Keine passenden Dokumente in Paperless gefunden."
            lines = [f"{data.get('count', len(docs))} Treffer in Paperless" +
                     (f" (zeige {len(docs)})" if data.get("count", 0) > len(docs) else "") + ":"]
            for d in docs:
                line = await pc.describe(d)
                hit = d.get("__search_hit__") or {}
                snippet = _strip_html(hit.get("highlights") or "") or _strip_html(d.get("content") or "")[:160]
                if snippet:
                    line += f"\n    „{snippet[:220]}“"
                lines.append(line)
            lines.append("Inhalt befragen: paperless_ask(document_id, question) · Öffnen: paperless_open(document_id)")
            return "\n".join(lines)

    return await _guard(run())


@tool("Beantwortet eine Frage zu einem bestimmten Paperless-Dokument: liefert die relevantesten Textstellen "
      "(OCR-Text) für die Frage. Beantworte die Frage dann aus diesen Stellen und nenne das Dokument.",
      enabled=_enabled)
async def paperless_ask(
    ctx: ToolContext,
    document_id: Annotated[int, "ID des Dokuments (aus paperless_search)"],
    question: Annotated[str, "Die Frage zum Dokument, z. B. 'Wann endet die Mindestlaufzeit?'"],
) -> str:
    async def run() -> str:
        async with PaperlessClient(ctx.cfg) as pc:
            doc = await pc.document(document_id)
            head = await pc.describe(doc)
            content = (doc.get("content") or "").strip()
            if not content:
                return f"{head}\n(Das Dokument enthält keinen erkannten Text – evtl. OCR in Paperless prüfen.)"
            budget = pc.max_chars
            if len(content) <= budget:
                return f"{head}\n--- vollständiger Text ---\n{content}"
            passages = split_passages(content)
            order = await rank_passages(question, passages, _embedder(ctx))
            chosen, used = [], 0
            for i in order:
                if used + len(passages[i]) > budget and chosen:
                    break
                chosen.append(i)
                used += len(passages[i])
            parts = [f"[Stelle {i + 1}/{len(passages)}]\n{passages[i]}" for i in sorted(chosen)]
            return (f"{head}\n--- die {len(chosen)} relevantesten von {len(passages)} Textstellen zur Frage "
                    f"„{question}“ ---\n" + "\n\n".join(parts) +
                    "\n(Steht die Antwort nicht darin: paperless_read für den ganzen Text.)")

    return await _guard(run())


@tool("Liest den vollständigen Text (OCR) eines Paperless-Dokuments abschnittsweise, z. B. zum Zusammenfassen "
      "oder Vorlesen.", enabled=_enabled)
async def paperless_read(
    ctx: ToolContext,
    document_id: Annotated[int, "ID des Dokuments"],
    offset: Annotated[int, "Ab diesem Zeichen weiterlesen (für lange Dokumente)"] = 0,
) -> str:
    async def run() -> str:
        async with PaperlessClient(ctx.cfg) as pc:
            doc = await pc.document(document_id)
            head = await pc.describe(doc)
            content = (doc.get("content") or "").strip()
            start = max(0, int(offset))
            part = content[start:start + pc.max_chars]
            rest = len(content) - start - len(part)
            more = f"\n… noch {rest} Zeichen – weiter mit offset={start + len(part)}" if rest > 0 else ""
            return f"{head}\n---\n{part or '(kein Text)'}{more}"

    return await _guard(run())


def _safe_name(doc_id: int, title: str, ext: str) -> str:
    stem = re.sub(r"[^\w\-. ]+", "_", title or "dokument").strip(" ._")[:80] or "dokument"
    return f"{doc_id}-{stem}{ext}"


@tool("Öffnet ein Paperless-Dokument lokal mit dem Standardprogramm (lädt das PDF herunter).", enabled=_enabled)
async def paperless_open(
    ctx: ToolContext,
    document_id: Annotated[int, "ID des Dokuments (aus paperless_search)"],
    original: Annotated[bool, "True = Originaldatei statt des archivierten PDFs"] = False,
) -> str:
    async def run() -> str:
        async with PaperlessClient(ctx.cfg) as pc:
            doc = await pc.document(document_id)
            ext = ".pdf"
            if original:
                ext = Path(doc.get("original_file_name") or "").suffix or ".pdf"
            target = cache_dir() / _safe_name(doc["id"], doc.get("title", ""), ext)
            if not target.exists() or target.stat().st_size == 0:
                r = await pc.request("GET", f"/documents/{doc['id']}/download/",
                                     params={"original": "true"} if original else None)
                if r.status_code >= 400:
                    raise PaperlessError(f"Download fehlgeschlagen ({r.status_code}).")
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(target.suffix + ".part")
                tmp.write_bytes(r.content)
                tmp.replace(target)
        opener = next((c for c in (["xdg-open"], ["gio", "open"]) if shutil.which(c[0])), None)
        if not opener:
            return f"Heruntergeladen nach {target}, aber xdg-open fehlt (Paket xdg-utils)."
        ok, err = await proc.launch([*opener, str(target)])
        if ok:
            return f"Geöffnet: „{doc.get('title')}“ ({target})"
        return f"Heruntergeladen nach {target}, Öffnen fehlgeschlagen ({err})."

    return await _guard(run())


async def paperless_status(cfg) -> dict:
    """Für jarvis doctor."""
    if not cfg.paperless.enabled:
        return {"enabled": False, "online": False}
    try:
        async with PaperlessClient(cfg) as pc:
            r = await pc.request("GET", "/documents/", params={"page_size": 1})
            if r.status_code >= 400:
                raise PaperlessError(f"Paperless-Fehler {r.status_code}")
            if html_instead_of_json(r):
                raise PaperlessError(f"Unter {pc.url} antwortet eine Webseite statt der Paperless-API.")
            return {"enabled": True, "online": True, "count": r.json().get("count", 0),
                    "version": r.headers.get("x-version", "?"), "url": pc.url}
    except (PaperlessError, ValueError) as e:
        return {"enabled": True, "online": False, "error": str(e), "url": normalize_url(cfg.paperless.url, "/api")}
