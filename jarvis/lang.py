"""Sprache der Kommandozeilen-Ausgaben (folgt der Config-Einstellung language)."""

_lang = "de"


def set_lang(language: str) -> None:
    global _lang
    _lang = "en" if language == "en" else "de"


def T(de: str, en: str) -> str:  # noqa: N802 – kurzer Name, wird überall in Ausgaben verwendet
    return en if _lang == "en" else de
