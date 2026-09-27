"""Websuche (DuckDuckGo via ddgs oder eigene SearXNG-Instanz) und Abruf von Webseiten."""

from __future__ import annotations

import asyncio
import re
import shutil
from typing import Annotated
from urllib.parse import quote_plus

import httpx

from . import proc
from .registry import ToolContext, tool

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# Name → (Startseite, Such-URL mit {q})
SITES: dict[str, tuple[str, str]] = {
    "youtube": ("https://www.youtube.com", "https://www.youtube.com/results?search_query={q}"),
    "google": ("https://www.google.de", "https://www.google.de/search?q={q}"),
    "duckduckgo": ("https://duckduckgo.com", "https://duckduckgo.com/?q={q}"),
    "wikipedia": ("https://de.wikipedia.org", "https://de.wikipedia.org/w/index.php?search={q}"),
    "amazon": ("https://www.amazon.de", "https://www.amazon.de/s?k={q}"),
    "ebay": ("https://www.ebay.de", "https://www.ebay.de/sch/i.html?_nkw={q}"),
    "kleinanzeigen": ("https://www.kleinanzeigen.de", "https://www.kleinanzeigen.de/s-{q}/k0"),
    "idealo": ("https://www.idealo.de", "https://www.idealo.de/preisvergleich/MainSearchProductCategory.html?q={q}"),
    "github": ("https://github.com", "https://github.com/search?q={q}"),
    "reddit": ("https://www.reddit.com", "https://www.reddit.com/search/?q={q}"),
    "maps": ("https://www.google.com/maps", "https://www.google.com/maps/search/{q}"),
    "openstreetmap": ("https://www.openstreetmap.org", "https://www.openstreetmap.org/search?query={q}"),
    "netflix": ("https://www.netflix.com", "https://www.netflix.com/search?q={q}"),
    "twitch": ("https://www.twitch.tv", "https://www.twitch.tv/search?term={q}"),
    "spotify": ("https://open.spotify.com", "https://open.spotify.com/search/{q}"),
    "chefkoch": ("https://www.chefkoch.de", "https://www.chefkoch.de/rs/s0/{q}/Rezepte.html"),
    "leo": ("https://dict.leo.org", "https://dict.leo.org/englisch-deutsch/{q}"),
    "deepl": ("https://www.deepl.com/translator", "https://www.deepl.com/translator#en/de/{q}"),
    "archwiki": ("https://wiki.archlinux.org", "https://wiki.archlinux.org/index.php?search={q}"),
    "aur": ("https://aur.archlinux.org", "https://aur.archlinux.org/packages?K={q}"),
    "heise": ("https://www.heise.de", "https://www.heise.de/suche/?q={q}"),
    "tagesschau": ("https://www.tagesschau.de", "https://www.tagesschau.de/suche?searchText={q}"),
    "wetter": ("https://www.wetter.com", "https://www.wetter.com/suche/?q={q}"),
}
ALIASES = {
    "yt": "youtube", "google maps": "maps", "karte": "maps", "karten": "maps", "osm": "openstreetmap",
    "wiki": "wikipedia", "ebay kleinanzeigen": "kleinanzeigen", "arch wiki": "archwiki",
    "amazon.de": "amazon", "youtube.com": "youtube",
}
DOMAIN_RE = re.compile(r"^(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?$", re.I)


def website_url(site: str, query: str = "", extra: dict[str, str] | None = None) -> str:
    """URL für eine bekannte Seite (optional mit Suche), eine Domain oder als Fallback die DuckDuckGo-Suche."""
    name = site.strip().lower()
    q = quote_plus(query.strip()) if query.strip() else ""
    for key, url in (extra or {}).items():
        if key.lower() == name:
            if "{q}" in url:
                return url.replace("{q}", q) if q else re.sub(r"^(https?://[^/]+).*$", r"\1", url)
            return url
    name = ALIASES.get(name, name)
    if name in SITES:
        home, search = SITES[name]
        return search.format(q=q) if q else home
    if DOMAIN_RE.match(name):
        url = name if name.startswith("http") else f"https://{name}"
        return url
    # Unbekannt: DuckDuckGo-„!ducky“ springt direkt zum ersten Treffer
    return "https://duckduckgo.com/?q=" + quote_plus(f"!ducky {site} {query}".strip())


@tool("Öffnet eine Website im Browser – eine bekannte Seite (YouTube, Amazon, Wikipedia, Google Maps, GitHub, "
      "Chefkoch, …), eine Domain wie heise.de oder direkt eine Suche auf der Seite.")
async def open_website(
    ctx: ToolContext,
    site: Annotated[str, "Name der Seite oder Domain, z. B. 'youtube', 'amazon', 'heise.de'"],
    query: Annotated[str, "Optional: Suchbegriff auf dieser Seite, z. B. 'lofi hip hop'"] = "",
) -> str:
    url = website_url(site, query, ctx.cfg.websites)
    opener = next((c for c in (["xdg-open"], ["gio", "open"]) if shutil.which(c[0])), None)
    if not opener:
        return f"Kein Programm zum Öffnen gefunden (xdg-utils fehlt). URL: {url}"
    ok, err = await proc.launch([*opener, url])
    if ok:
        return f"Geöffnet: {url}"
    hint = "" if proc.has_display() else " Jarvis hat keinen Zugriff auf die grafische Sitzung."
    return f"Öffnen fehlgeschlagen ({err}).{hint} URL: {url}"


async def _searxng(url: str, query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{url.rstrip('/')}/search", params={"q": query, "format": "json", "language": "de"})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "href": x.get("url", ""), "body": x.get("content", "")}
                for x in r.json().get("results", [])[:n]]


def _ddgs(query: str, n: int) -> list[dict]:
    from ddgs import DDGS
    return DDGS().text(query, region="de-de", max_results=n) or []


@tool("Sucht im Internet und liefert Titel, URL und Kurzbeschreibung der Treffer. Für Details danach fetch_url.")
async def web_search(
    ctx: ToolContext,
    query: Annotated[str, "Suchanfrage"],
    max_results: Annotated[int, "Anzahl Treffer (Standard 5)"] = 5,
) -> str:
    n = max(1, min(max_results, 10))
    try:
        if ctx.cfg.tools.searxng_url:
            results = await _searxng(ctx.cfg.tools.searxng_url, query, n)
        else:
            results = await asyncio.to_thread(_ddgs, query, n)
    except Exception as e:  # noqa: BLE001
        return f"Websuche fehlgeschlagen: {e}"
    if not results:
        return "Keine Suchergebnisse."
    return "\n\n".join(f"{i}. {r.get('title', '')}\n{r.get('href', '')}\n{r.get('body', '')}"
                       for i, r in enumerate(results, 1))


@tool("Ruft eine Webseite ab und gibt den lesbaren Haupttext zurück.")
async def fetch_url(ctx: ToolContext, url: Annotated[str, "Vollständige URL (http/https)"]) -> str:
    if not url.startswith(("http://", "https://")):
        return "Nur http(s)-URLs werden unterstützt."
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA}) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return f"Abruf fehlgeschlagen: {e}"
    if r.status_code >= 400:
        return f"Abruf fehlgeschlagen: HTTP {r.status_code}"
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype and "json" not in ctype:
        return f"Kein Textinhalt ({ctype})."
    text = r.text
    if "html" in ctype:
        import trafilatura
        text = await asyncio.to_thread(trafilatura.extract, r.text, include_links=False) or ""
    return proc.clip(text.strip() or "(kein lesbarer Text gefunden)", ctx.cfg.tools.max_output_chars)
