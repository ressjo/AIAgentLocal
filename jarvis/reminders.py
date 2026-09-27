"""Erinnerungen und Timer – dauerhaft gespeichert in ~/.local/share/jarvis/reminders.json."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class Reminder:
    id: str
    text: str
    due: str            # ISO-Zeitpunkt (lokale Zeit)
    kind: str = "reminder"  # reminder | timer
    created: str = ""
    done: bool = False

    @property
    def due_dt(self) -> datetime:
        return datetime.fromisoformat(self.due)

    def label(self) -> str:
        when = self.due_dt
        today = datetime.now().date()
        if when.date() == today:
            day = "heute"
        elif when.date() == today + timedelta(days=1):
            day = "morgen"
        else:
            day = when.strftime("%d.%m.%Y")
        kind = "Timer" if self.kind == "timer" else "Erinnerung"
        return f"{kind} {day} um {when.strftime('%H:%M')} Uhr: {self.text} (ID {self.id})"


class ReminderStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.items: list[Reminder] = []
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self.items = [Reminder(**r) for r in json.loads(self.path.read_text(encoding="utf-8"))]
            except (json.JSONDecodeError, TypeError, OSError):
                self.items = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in self.items], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, text: str, due: datetime, kind: str = "reminder") -> Reminder:
        with self._lock:
            r = Reminder(id=uuid.uuid4().hex[:6], text=text.strip(), due=due.isoformat(timespec="seconds"),
                         kind=kind, created=datetime.now().isoformat(timespec="seconds"))
            self.items.append(r)
            # Erledigte Einträge älter als 7 Tage aufräumen
            cutoff = datetime.now() - timedelta(days=7)
            self.items = [x for x in self.items if not x.done or x.due_dt > cutoff]
            self.save()
            return r

    def upcoming(self) -> list[Reminder]:
        return sorted((r for r in self.items if not r.done), key=lambda r: r.due)

    def today(self) -> list[Reminder]:
        today = datetime.now().date()
        return [r for r in self.upcoming() if r.due_dt.date() == today]

    def due(self, now: datetime | None = None) -> list[Reminder]:
        now = now or datetime.now()
        return [r for r in self.upcoming() if r.due_dt <= now]

    def mark_done(self, rid: str) -> None:
        with self._lock:
            for r in self.items:
                if r.id == rid:
                    r.done = True
            self.save()

    def cancel(self, which: str) -> list[Reminder]:
        """Per ID, Textbestandteil oder 'alle'."""
        w = which.strip().lower()
        with self._lock:
            open_items = [r for r in self.items if not r.done]
            if w in ("alle", "all", "*"):
                hit = open_items
            else:
                hit = [r for r in open_items if r.id == w] or [r for r in open_items if w and w in r.text.lower()]
            for r in hit:
                self.items.remove(r)
            if hit:
                self.save()
            return hit


TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")


def parse_when(in_minutes: float = 0, at: str = "", now: datetime | None = None) -> datetime:
    """Relativ (Minuten) oder absolut: 'YYYY-MM-DD HH:MM', 'DD.MM.YYYY HH:MM', 'DD.MM. HH:MM' oder nur 'HH:MM'
    (heute, bzw. morgen falls schon vorbei)."""
    now = now or datetime.now()
    if in_minutes and in_minutes > 0:
        return now + timedelta(minutes=in_minutes)
    text = at.strip().replace("T", " ").removesuffix(" Uhr").strip()
    if not text:
        raise ValueError("Bitte eine Zeit angeben (in_minutes oder at).")
    m = TIME_RE.match(text)
    if m:
        due = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return due if due > now else due + timedelta(days=1)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y %H.%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.?\s+(\d{1,2})[:.](\d{2})$", text)
    if m:
        due = datetime(now.year, int(m.group(2)), int(m.group(1)), int(m.group(3)), int(m.group(4)))
        return due if due > now else due.replace(year=now.year + 1)
    raise ValueError(f"Zeitangabe '{at}' nicht verstanden (Format z. B. '2026-10-01 14:30' oder '14:30').")
