"""Paperless-ngx über die REST-API: Dokumente suchen, befragen, lesen und lokal als PDF öffnen; Metadaten
(Korrespondent, Dokumenttyp, Tags, Titel, Datum) vorschlagen und nach Bestätigung übernehmen.

Einrichtung: in Paperless oben rechts auf das Profil → „API-Auth-Token“ erzeugen und in der Config eintragen:
    paperless:
      url: http://nas.local:8000
      token: "…"
"""

from __future__ import annotations

import difflib
import html
import json
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import Annotated, Any

import httpx

from ..lang import T
from . import proc
from .netutil import client_kwargs, explain, html_instead_of_json, normalize_url
from .registry import BLOCKED, CONFIRM, SAFE, ToolContext, tool
from .secretpaths import is_secret_path, secret_reason

# Für Tests austauschbar (httpx.MockTransport)
TRANSPORT: httpx.AsyncBaseTransport | None = None

KINDS = {"correspondent": "correspondents", "document_type": "document_types", "tag": "tags"}
KIND_LABEL = {"correspondents": "Korrespondent", "document_types": "Dokumenttyp", "tags": "Tag"}
# Zuletzt geladene Namen je Art – damit die (synchrone) Bestätigungsübersicht neue Einträge erkennt
_KNOWN: dict[str, list[str]] = {}


class PaperlessError(RuntimeError):
    pass


def _enabled(cfg: Any) -> bool:
    return cfg.paperless.enabled


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "orbwise" / "paperless"


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
            _KNOWN[kind] = list(self._names[kind].values())
        return self._names[kind]

    async def write(self, method: str, path: str, body: dict) -> dict:
        r = await self.request(method, path, json=body)
        if r.status_code >= 400:
            detail = r.text[:300]
            try:
                detail = "; ".join(f"{k}: {v}" for k, v in r.json().items()) or detail
            except (ValueError, AttributeError):
                pass
            raise PaperlessError(f"Paperless hat die Änderung abgelehnt ({r.status_code}): {detail}")
        return r.json() if r.content else {}

    async def upload(self, filename: str, data: bytes, content_type: str = "application/pdf", title: str = "") -> str:
        """Dokument hochladen (POST /documents/post_document/) – liefert die Aufgaben-ID; Paperless verarbeitet es
        danach im Hintergrund."""
        r = await self.request("POST", "/documents/post_document/", files={"document": (filename, data, content_type)},
                               data={"title": title} if title.strip() else None)
        if r.status_code >= 400:
            raise PaperlessError(f"Paperless hat den Upload abgelehnt ({r.status_code}): {r.text[:200]}")
        try:
            return str(r.json())
        except ValueError:
            return r.text.strip().strip('"')

    async def resolve(self, kind: str, name: str, created: list[str]) -> int:
        """ID zu einem Namen (Groß-/Kleinschreibung egal); fehlt er, wird er angelegt (Bestätigung liegt vor)."""
        names = await self.names(kind)
        wanted = name.strip().casefold()
        for id_, existing in names.items():
            if existing.casefold() == wanted:
                return id_
        new = await self.write("POST", f"/{kind}/", {"name": name.strip()})
        names[new["id"]] = new.get("name", name.strip())
        _KNOWN[kind] = list(names.values())
        created.append(f"{KIND_LABEL[kind]} „{name.strip()}“")
        return new["id"]

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


async def download_document(cfg: Any, document_id: int, original: bool = False) -> tuple[str, bytes]:
    """PDF (bzw. Original) eines Dokuments holen – liefert (Dateiname, Inhalt)."""
    async with PaperlessClient(cfg) as pc:
        doc = await pc.document(document_id)
        ext = (Path(doc.get("original_file_name") or "").suffix or ".pdf") if original else ".pdf"
        r = await pc.request("GET", f"/documents/{doc['id']}/download/", params={"original": "true"} if original else None)
        if r.status_code >= 400:
            raise PaperlessError(f"Download fehlgeschlagen ({r.status_code}).")
        return _safe_name(doc["id"], doc.get("title", ""), ext), r.content


UPLOAD_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".txt", ".eml",
                ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp"}
MAX_UPLOAD = 200_000_000


def _in_telegram_inbox(cfg: Any, path: Path) -> bool:
    tg = getattr(cfg, "telegram", None)
    if tg is None:
        return False
    try:
        return path.resolve().is_relative_to(tg.inbox.resolve())
    except OSError:
        return False


def _upload_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    path = Path(str(args.get("path") or "")).expanduser()
    if is_secret_path(path):
        return BLOCKED, secret_reason()
    if _in_telegram_inbox(ctx.cfg, path):
        return SAFE, ""  # selbst vom Handy geschickt → direkt ablegen
    return CONFIRM, T(f"Datei {path} an Paperless übergeben", f"hand file {path} over to Paperless")


@tool("Legt eine lokale Datei (PDF, Foto, Office-Dokument) in Paperless ab – z. B. eine Datei, die der Nutzer per "
      "Telegram geschickt hat. Paperless erkennt Text und Metadaten danach im Hintergrund.",
      risk=_upload_risk, enabled=_enabled)
async def paperless_upload(
    ctx: ToolContext,
    path: Annotated[str, "Pfad der Datei"],
    title: Annotated[str, "Titel (optional; leer = Paperless wählt selbst)"] = "",
) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"Datei nicht gefunden: {p}"
    if p.suffix.lower() not in UPLOAD_TYPES:
        return f"Paperless nimmt {p.suffix or 'diese Dateien'} nicht an (PDF, Bilder, Office-Dokumente gehen)."
    if p.stat().st_size > MAX_UPLOAD:
        return "Die Datei ist zu groß (höchstens 200 MB)."

    async def run() -> str:
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        async with PaperlessClient(ctx.cfg) as pc:
            task = await pc.upload(p.name, p.read_bytes(), ctype, title)
        return f"An Paperless übergeben: {p.name} (Aufgabe {task}) – Paperless verarbeitet es gleich."

    return await _guard(run())


# ---------------------------------------------------------------- Metadaten vorschlagen und übernehmen

MAX_SUGGEST, MAX_APPLY, MAX_LISTED = 5, 25, 150
REVIEW_BATCH = 3  # Dokumente pro Paket beim Sortieren – danach Pause
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _load(value: Any) -> Any:
    """Listen kommen vom Modell mal als Liste, mal als JSON-Text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _ids(value: Any) -> list[int]:
    value = _load(value)
    items = re.findall(r"\d+", value) if isinstance(value, str) else value if isinstance(value, list) else [value]
    out: list[int] = []
    for x in items:
        try:
            if int(x) not in out:
                out.append(int(x))
        except (TypeError, ValueError):
            pass
    return out


def _changes(value: Any) -> list[dict]:
    value = _load(value)
    if isinstance(value, dict):
        value = [value]
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def _names_list(value: Any) -> list[str]:
    value = _load(value)
    if isinstance(value, str):
        value = value.split(",")
    return [str(x).strip() for x in value or [] if str(x).strip()] if isinstance(value, list) else []


def _summary(c: dict) -> str:
    parts = []
    if str(c.get("title") or "").strip():
        parts.append(f"Titel → „{str(c['title']).strip()}“")
    if str(c.get("created") or "").strip():
        parts.append(f"Datum → {str(c['created']).strip()}")
    if str(c.get("correspondent") or "").strip():
        parts.append(f"Korrespondent → {str(c['correspondent']).strip()}")
    if str(c.get("document_type") or "").strip():
        parts.append(f"Typ → {str(c['document_type']).strip()}")
    parts += [f"+Tag {t}" for t in _names_list(c.get("add_tags"))]
    parts += [f"−Tag {t}" for t in _names_list(c.get("remove_tags"))]
    return " · ".join(parts) or "keine Änderung"


def _new_entry(kind: str, name: str) -> str:
    """„Korrespondent ‚X‘ (ähnlich: ‚Y‘)“, wenn der Name noch nicht existiert – sonst ''."""
    known = _KNOWN.get(kind)
    label = KIND_LABEL[kind]
    if known is None:
        return "?"  # Namen noch nicht geladen
    if any(k.casefold() == name.casefold() for k in known):
        return ""
    lower = {k.casefold(): k for k in known}
    close = difflib.get_close_matches(name.casefold(), list(lower), n=1, cutoff=0.6)
    close = close or [k for k in lower if len(k) > 2 and (k in name.casefold() or name.casefold() in k)][:1]
    return f"{label} „{name}“" + (f" (ähnlich vorhanden: „{lower[close[0]]}“)" if close else "")


def _apply_risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
    changes = _changes(args.get("changes"))
    lines = [f"Dok {c.get('document_id')}: {_summary(c)}" for c in changes[:MAX_APPLY]]
    new: list[str] = []
    for c in changes:
        wanted = [("correspondents", str(c.get("correspondent") or "").strip()),
                  ("document_types", str(c.get("document_type") or "").strip())]
        wanted += [("tags", t) for t in _names_list(c.get("add_tags"))]
        for kind, name in wanted:
            note = _new_entry(kind, name) if name else ""
            if note and note not in new:
                new.append(note)
    n = len(changes)
    reason = T(f"Paperless-Metadaten ändern ({n} Dokument{'e' if n != 1 else ''})",
               f"change Paperless metadata ({n} document{'s' if n != 1 else ''})") + ":\n" + "\n".join(lines)
    if "?" in new:
        reason += T("\nFehlende Korrespondenten/Typen/Tags werden neu angelegt (vorhandene Namen noch nicht geprüft).",
                    "\nMissing correspondents/types/tags will be created (existing names not checked yet).")
    elif new:
        reason += T("\nNEU anlegen: ", "\nCREATE: ") + "; ".join(new)
    return CONFIRM, reason


async def _suggest_material(pc: "PaperlessClient", ids: list[int]) -> str:
    """Stand, Vorschläge von Paperless und Textauszug je Dokument plus die vorhandenen Namen."""
    known = {kind: await pc.names(kind) for kind in ("correspondents", "document_types", "tags")}
    budget = max(300, pc.max_chars // len(ids))
    blocks = []
    for doc_id in ids:
        try:
            doc = await pc.document(doc_id)
        except PaperlessError as e:
            blocks.append(f"[{doc_id}] ✘ {e}")
            continue
        lines = [await pc.describe(doc)]
        try:
            sug = await pc.json(f"/documents/{doc_id}/suggestions/") or {}
        except PaperlessError:
            sug = {}  # ältere Paperless-Versionen / Klassifikator noch nicht trainiert
        hints = []
        for key, label in (("correspondents", "Korrespondent"), ("document_types", "Typ"), ("tags", "Tags")):
            names = [known[key][x] for x in sug.get(key) or [] if x in known[key]]
            if names:
                hints.append(f"{label}: {', '.join(names)}")
        if sug.get("dates"):
            hints.append("Daten im Text: " + ", ".join(str(d)[:10] for d in sug["dates"][:5]))
        if hints:
            lines.append("  Paperless schlägt vor – " + " · ".join(hints))
        text = re.sub(r"\s+", " ", doc.get("content") or "").strip()
        lines.append(f"  Text: „{text[:budget]}{'…' if len(text) > budget else ''}“" if text
                     else "  (kein erkannter Text – Vorschlag nur aus Titel/Dateiname möglich: "
                          f"{doc.get('original_file_name') or '?'})")
        blocks.append("\n".join(lines))
    listing = []
    for kind, label in (("correspondents", "Korrespondenten"), ("document_types", "Dokumenttypen"),
                        ("tags", "Tags")):
        names = sorted(known[kind].values(), key=str.casefold)
        more = f" … (+{len(names) - MAX_LISTED})" if len(names) > MAX_LISTED else ""
        listing.append(f"Vorhandene {label} ({len(names)}): " + (", ".join(names[:MAX_LISTED]) or "–") + more)
    return "\n\n".join(blocks) + "\n\n" + "\n".join(listing)


@tool("Sammelt alles, um Korrespondent, Dokumenttyp, Tags, Titel und Datum für Paperless-Dokumente vorzuschlagen: "
      "aktueller Stand, Vorschläge von Paperless, Textauszug und die vorhandenen Korrespondenten/Typen/Tags. "
      "Danach den Vorschlag als Liste zeigen und mit paperless_apply_metadata übernehmen.", enabled=_enabled)
async def paperless_suggest_metadata(
    ctx: ToolContext,
    document_ids: Annotated[list[int], f"IDs der Dokumente aus paperless_search (höchstens {MAX_SUGGEST}), z. B. [7, 8]"],
) -> str:
    async def run() -> str:
        ids = _ids(document_ids)
        if not ids:
            return "Bitte Dokument-IDs angeben (z. B. aus paperless_search)."
        skipped = ids[MAX_SUGGEST:]
        ids = ids[:MAX_SUGGEST]
        async with PaperlessClient(ctx.cfg) as pc:
            out = await _suggest_material(pc, ids)
        if skipped:
            out += f"\n(Nur die ersten {MAX_SUGGEST} Dokumente – danach mit {skipped[:MAX_SUGGEST]} weitermachen.)"
        return out + ("\nNächster Schritt: Vorschlag pro Dokument als kurze Liste zeigen (vorhandene Namen exakt so "
                      "schreiben, neue nur wenn nichts passt), dann paperless_apply_metadata mit allen Dokumenten "
                      "aufrufen – der Nutzer bestätigt dort.")

    return await _guard(run())


async def _apply_one(pc: PaperlessClient, c: dict, created: list[str]) -> str:
    try:
        doc_id = int(c.get("document_id"))
    except (TypeError, ValueError):
        raise PaperlessError(f"ungültige document_id {c.get('document_id')!r}") from None
    date = str(c.get("created") or "").strip()
    if date and not DATE_RE.fullmatch(date):
        raise PaperlessError(f"Dok {doc_id}: Datum bitte als YYYY-MM-DD (nicht '{date}') – nichts geändert")
    doc = await pc.document(doc_id)
    fields: dict[str, Any] = {}
    title = str(c.get("title") or "").strip()
    if title and title != doc.get("title"):
        fields["title"] = title
    if date and date != str(doc.get("created") or "")[:10]:
        fields["created"] = date
    for key, kind in (("correspondent", "correspondents"), ("document_type", "document_types")):
        name = str(c.get(key) or "").strip()
        if name:
            new_id = await pc.resolve(kind, name, created)
            if new_id != doc.get(key):
                fields[key] = new_id
    tags = list(doc.get("tags") or [])
    for name in _names_list(c.get("add_tags")):
        tag_id = await pc.resolve("tags", name, created)
        if tag_id not in tags:
            tags.append(tag_id)
    known_tags = {v.casefold(): k for k, v in (await pc.names("tags")).items()}
    for name in _names_list(c.get("remove_tags")):
        tag_id = known_tags.get(name.casefold())
        if tag_id in tags:
            tags.remove(tag_id)
    if tags != list(doc.get("tags") or []):
        fields["tags"] = tags
    label = f"Dok {doc_id} „{fields.get('title') or doc.get('title') or ''}“"
    if not fields:
        return f"– {label}: schon so eingetragen, nichts geändert"
    try:
        await pc.write("PATCH", f"/documents/{doc_id}/", fields)
    except PaperlessError as e:
        if "created" not in fields or "created" not in str(e):
            raise
        # ältere Paperless-Versionen: Datum nur über created_date änderbar
        fields["created_date"] = fields.pop("created")
        await pc.write("PATCH", f"/documents/{doc_id}/", fields)
    return f"✔ {label}: {_summary(c)}"


@tool("Übernimmt Metadaten für ein oder mehrere Paperless-Dokumente nach Bestätigung durch den Nutzer: "
      "Korrespondent, Dokumenttyp, Tags hinzufügen/entfernen, Titel, Datum. Fehlende Korrespondenten/Typen/Tags "
      "werden angelegt. Vorher paperless_suggest_metadata nutzen.", risk=_apply_risk, enabled=_enabled)
async def paperless_apply_metadata(
    ctx: ToolContext,
    changes: Annotated[list[dict], "Eine Änderung pro Dokument: {document_id, title?, created? (YYYY-MM-DD), "
                                   "correspondent?, document_type?, add_tags? [..], remove_tags? [..]} – "
                                   "nur Felder angeben, die sich ändern sollen"],
) -> str:
    async def run() -> str:
        items = _changes(changes)
        if not items:
            return "Keine Änderungen übergeben (changes = Liste mit {document_id, …})."
        if len(items) > MAX_APPLY:
            return f"Höchstens {MAX_APPLY} Dokumente auf einmal – bitte aufteilen."
        results, created = [], []
        async with PaperlessClient(ctx.cfg) as pc:
            for c in items:
                try:
                    results.append(await _apply_one(pc, c, created))
                except PaperlessError as e:
                    results.append(f"✘ {e}")
        if created:
            results.append("Neu angelegt: " + ", ".join(created))
        ok = [int(c["document_id"]) for c, r in zip(items, results) if r.startswith(("✔", "–"))]
        ledger = mark_reviewed(ctx.cfg, ok)
        if ledger and set(ok) & set(ledger.get("batch") or []):
            done, skipped = len(ledger.get("done", [])), len(ledger.get("skipped", []))
            results.append(f"Sortier-Durchgang: {done} erledigt" + (f", {skipped} übersprungen" if skipped else "")
                           + ". Paket beendet – NICHT selbst weitermachen: dem Nutzer kurz den Stand sagen und "
                             "fragen, ob es mit dem nächsten Paket weitergehen soll.")
        return "\n".join(results)

    return await _guard(run())


# ---------------------------------------------------------------- Sortier-Durchgang (Paket für Paket)
# Viele Dokumente einordnen: Der Fortschritt steht in einer Datei statt nur im Chat (überlebt Kürzen, Komprimieren
# und Neustarts), es gibt immer nur ein kleines Paket pro Nutzernachricht – danach hält Jarvis an und fragt.

def _ledger_path(cfg: Any) -> Path:
    return Path(cfg.memory.dir) / "paperless-review.json"


def _ledger_load(cfg: Any) -> dict:
    try:
        data = json.loads(_ledger_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ledger_save(cfg: Any, data: dict) -> None:
    path = _ledger_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["updated"] = time.time()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _turn_key(ctx: ToolContext) -> list | None:
    """Welche Nutzernachricht gerade bearbeitet wird – pro Nachricht gibt es nur ein Paket."""
    conv = ctx.memory.conversation if ctx.memory else None
    if conv is None:
        return None
    return [conv.chat_id, sum(1 for m in conv.history if m.get("role") == "user")]


async def _inbox_filter(pc: "PaperlessClient", tag: str) -> dict | str:
    """Dokument-Filter für den Posteingang (Tag aus der Config, sonst Paperless' Posteingangs-Tags) – oder ein
    Hinweis, wenn keiner festgelegt ist."""
    if tag.strip():
        return {"tags__name__iexact": tag.strip()}
    inbox = await pc.json("/tags/", is_inbox_tag="true", page_size=100) or {}
    ids = [str(t["id"]) for t in inbox.get("results", []) if t.get("is_inbox_tag", True)]
    if not ids:
        return ("kein Posteingangs-Tag festgelegt (in Paperless beim Tag „Posteingangs-Tag“ anhaken oder "
                "briefing.inbox_tag setzen) – sonst scope='incomplete' nutzen")
    return {"tags__id__in": ",".join(ids)}


async def _queue(pc: "PaperlessClient", cfg: Any, scope: str) -> list[int] | str:
    """Alle Dokumente des Durchgangs (älteste zuerst): Posteingang oder ohne Korrespondent/Dokumenttyp."""
    base = {"page_size": 1000, "ordering": "added", "truncate_content": "true"}
    if scope == "incomplete":
        ids: list[int] = []
        for flt in ({"correspondent__isnull": "true"}, {"document_type__isnull": "true"}):
            data = await pc.json("/documents/", **base, **flt) or {}
            ids += [d["id"] for d in data.get("results", []) if d["id"] not in ids]
        return sorted(ids)
    flt = await _inbox_filter(pc, getattr(getattr(cfg, "briefing", None), "inbox_tag", "") or "")
    if isinstance(flt, str):
        return flt
    data = await pc.json("/documents/", **base, **flt) or {}
    return [d["id"] for d in data.get("results", [])]


def _progress(ledger: dict, open_ids: list[int]) -> str:
    done, skipped = len(ledger.get("done", [])), len(ledger.get("skipped", []))
    total = done + skipped + len(open_ids)
    return f"{done} von {total} erledigt" + (f", {skipped} übersprungen" if skipped else "") + \
        f", {len(open_ids)} offen"


def mark_reviewed(cfg: Any, ids: list[int], key: str = "done") -> dict | None:
    """Dokumente im laufenden Durchgang als erledigt/übersprungen vermerken; liefert das Ledger (oder None)."""
    ledger = _ledger_load(cfg)
    if not ledger or not ids:
        return None
    for i in ids:
        for k in ("done", "skipped"):
            if i in ledger.get(k, []) and k != key:
                ledger[k].remove(i)
        if i not in ledger.setdefault(key, []):
            ledger[key].append(i)
    _ledger_save(cfg, ledger)
    return ledger


@tool("Sortier-Durchgang für viele Dokumente (Posteingang oder Dokumente ohne Korrespondent/Typ): liefert das "
      f"nächste Paket von {REVIEW_BATCH} Dokumenten mit allem für den Vorschlag und den Fortschritt. Der Fortschritt "
      "wird gespeichert – „weiter“ macht genau dort weiter. Pro Nutzernachricht nur ein Paket, danach anhalten.",
      enabled=_enabled)
async def paperless_review_next(
    ctx: ToolContext,
    scope: Annotated[str, "inbox = Posteingang (Standard), incomplete = ohne Korrespondent oder Dokumenttyp"] = "inbox",
) -> str:
    async def run() -> str:
        scope_ = "incomplete" if "incomplete" in (scope or "").lower() else "inbox"
        ledger = _ledger_load(ctx.cfg)
        if ledger.get("scope") != scope_:
            ledger = {"scope": scope_, "done": [], "skipped": [], "batch": [], "started": time.time()}
        turn = _turn_key(ctx)
        if turn is not None and ledger.get("batch_turn") == turn and ledger.get("batch"):
            return ("PAUSE: Für diese Nachricht gab es schon ein Paket (Dok "
                    f"{', '.join(map(str, ledger['batch']))}). Nicht selbst weitermachen – zeig dem Nutzer den Stand "
                    "und frag, ob es weitergehen soll.")
        async with PaperlessClient(ctx.cfg) as pc:
            queue = await _queue(pc, ctx.cfg, scope_)
            if isinstance(queue, str):
                return f"Paperless-Posteingang: {queue}."
            seen = set(ledger.get("done", [])) | set(ledger.get("skipped", []))
            open_ids = [i for i in queue if i not in seen]
            if not open_ids:
                total = len(ledger.get("done", [])) + len(ledger.get("skipped", []))
                _ledger_path(ctx.cfg).unlink(missing_ok=True)
                return (f"Fertig – alle {total} Dokumente des Durchgangs bearbeitet." if total
                        else "Nichts zu tun – keine passenden Dokumente.")
            batch = open_ids[:REVIEW_BATCH]
            material = await _suggest_material(pc, batch)
        ledger.update(batch=batch, batch_turn=turn)
        _ledger_save(ctx.cfg, ledger)
        head = f"Paket: Dok {', '.join(map(str, batch))} · Stand: {_progress(ledger, open_ids)}"
        return (f"{head}\n\n{material}\n\nSo weiter: Vorschlag pro Dokument als kurze Liste zeigen (vorhandene Namen "
                "exakt so schreiben, neue nur wenn nichts passt), dann paperless_apply_metadata für genau diese "
                f"{len(batch)} Dokumente – Unklares mit paperless_review_skip überspringen. Danach anhalten.")

    return await _guard(run())


@tool("Überspringt Dokumente im Sortier-Durchgang (z. B. unleserlich oder unklar) – sie kommen nicht wieder.",
      enabled=_enabled)
async def paperless_review_skip(
    ctx: ToolContext,
    document_ids: Annotated[list[int], "IDs der Dokumente, z. B. [12]"],
    reason: Annotated[str, "Kurzer Grund"] = "",
) -> str:
    ids = _ids(document_ids)
    ledger = mark_reviewed(ctx.cfg, ids, "skipped")
    if ledger is None:
        return "Kein laufender Sortier-Durchgang (erst paperless_review_next)."
    return f"Übersprungen: {', '.join(map(str, ids))}" + (f" ({reason.strip()})" if reason.strip() else "") + "."


async def inbox_summary(cfg, tag: str = "", limit: int = 5) -> str | None:
    """Für das Briefing: Dokumente im Posteingang (Tag aus der Config, sonst Paperless' Posteingangs-Tags)."""
    if not cfg.paperless.enabled:
        return None
    try:
        async with PaperlessClient(cfg) as pc:
            params: dict[str, Any] = {"page_size": limit, "ordering": "-added", "truncate_content": "true"}
            if tag.strip():
                params["tags__name__iexact"] = tag.strip()
            else:
                inbox = await pc.json("/tags/", is_inbox_tag="true", page_size=100) or {}
                ids = [str(t["id"]) for t in inbox.get("results", []) if t.get("is_inbox_tag", True)]
                if not ids:
                    return ("Paperless-Posteingang: kein Posteingangs-Tag festgelegt (in Paperless beim Tag "
                            "„Posteingangs-Tag“ anhaken oder briefing.inbox_tag setzen).")
                params["tags__id__in"] = ",".join(ids)
            data = await pc.json("/documents/", **params) or {}
    except PaperlessError as e:
        return f"Paperless-Posteingang: {e}"
    count, docs = data.get("count", 0), data.get("results", [])
    if not count:
        return "Paperless-Posteingang: leer."
    titles = ", ".join(f"„{d.get('title') or '?'}“ [{d['id']}]" for d in docs)
    more = f" und {count - len(docs)} weitere" if count > len(docs) else ""
    return (f"Paperless-Posteingang: {count} Dokument{'e' if count != 1 else ''} – {titles}{more}. "
            "(Auf Wunsch einordnen: paperless_review_next)")


async def paperless_status(cfg) -> dict:
    """Für orbwise doctor."""
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
