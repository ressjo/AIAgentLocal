"""Websuche (DuckDuckGo via ddgs oder eigene SearXNG-Instanz) und Abruf von Webseiten."""

from __future__ import annotations

import asyncio
from typing import Annotated

import httpx

from . import proc
from .registry import ToolContext, tool

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


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
