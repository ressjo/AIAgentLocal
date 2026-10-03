"""Routinen: Aufgaben, die Orbwise zu festen Zeiten selbst erledigt (z. B. „werktags um 8 nach Linux-News suchen“).

Gespeichert in ~/.local/share/orbwise/routines.json. Ausgeführt vom Server (routine_loop), jede Routine in ihrem
eigenen Chat. Verpasste Termine (PC aus) werden nur nachgeholt, wenn sie höchstens CATCH_UP her sind.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, time, timedelta
from pathlib import Path

CATCH_UP = timedelta(minutes=60)
DAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
DAY_NAMES_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_WORDS = {
    0: ("mo", "mon", "montag", "monday"), 1: ("di", "tue", "dienstag", "tuesday"),
    2: ("mi", "wed", "mittwoch", "wednesday"), 3: ("do", "thu", "donnerstag", "thursday"),
    4: ("fr", "fri", "freitag", "friday"), 5: ("sa", "sat", "samstag", "saturday", "sonnabend"),
    6: ("so", "sun", "sonntag", "sunday"),
}
TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?(?:\s*uhr)?$", re.I)


def parse_time(text: str) -> str:
    """'8', '8:30', '08.30 Uhr' → 'HH:MM'."""
    m = TIME_RE.match(str(text).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ValueError(f"Uhrzeit '{text}' nicht verstanden – bitte als HH:MM, z. B. 08:00.")
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


def parse_days(value) -> list[int]:
    """Wochentage aus Liste oder Text: 'werktags', 'Mo-Fr', 'Wochenende', 'täglich', 'mo, mi, fr', [0, 2, 4]."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        out = set()
        for x in value:
            out.update(parse_days(x) if isinstance(x, str) else [int(x)])
        if any(d < 0 or d > 6 for d in out):
            raise ValueError("Wochentage als 0 (Mo) bis 6 (So) angeben.")
        return sorted(out)
    if isinstance(value, int):
        return parse_days([value])
    text = str(value).strip().lower()
    if not text or re.fullmatch(r"(täglich|taeglich|jeden tag|daily|every ?day|alle|all|immer)", text):
        return []
    if re.fullmatch(r"(werktags?|werktage|wochentags?|weekdays?|mo\w*\s*(-|–|bis|to)\s*fr\w*)", text):
        return [0, 1, 2, 3, 4]
    if re.fullmatch(r"(am )?(wochenende|weekends?)", text):
        return [5, 6]
    if text.isdigit():
        return parse_days([int(text)])
    out: set[int] = set()
    text = re.sub(r"\s*(?:\bbis\b|\bto\b|-|–)\s*", "-", text)  # „Di bis Do“ → „di-do“
    for part in re.split(r"[,;/ ]+|\bund\b|\band\b", text):
        part = part.strip().rstrip("s.")  # „montags“ → „montag“
        if not part:
            continue
        rng = re.fullmatch(r"(\w+)\s*(?:-|–|bis)\s*(\w+)", part)
        if rng:
            a, b = _day(rng.group(1)), _day(rng.group(2))
            out.update(range(a, b + 1) if a <= b else [*range(a, 7), *range(0, b + 1)])
        else:
            out.add(_day(part))
    return sorted(out)


def _day(word: str) -> int:
    w = word.strip().lower()
    for i, words in _DAY_WORDS.items():
        if w in words or (len(w) >= 3 and any(x.startswith(w) for x in words)):
            return i
    raise ValueError(f"Wochentag '{word}' nicht verstanden (z. B. Mo, Di, werktags, Wochenende).")


def days_label(days: list[int], en: bool = False) -> str:
    names = DAY_NAMES_EN if en else DAY_NAMES
    if not days or len(days) == 7:
        return "daily" if en else "täglich"
    if days == [0, 1, 2, 3, 4]:
        return f"{names[0]}–{names[4]}"
    if days == [5, 6]:
        return "weekends" if en else "am Wochenende"
    runs, start = [], days[0]
    for a, b in zip(days, [*days[1:], None], strict=True):
        if b != a + 1:
            runs.append(names[start] if start == a else f"{names[start]}–{names[a]}" if a - start > 1
                        else f"{names[start]}, {names[a]}")
            start = b
    return ", ".join(runs)


@dataclass
class Routine:
    id: str
    name: str
    task: str
    time: str                        # HH:MM
    days: list[int] = field(default_factory=list)  # 0 = Mo … 6 = So; leer = täglich
    date: str = ""                   # YYYY-MM-DD = einmalig
    enabled: bool = True
    created: str = ""
    chat_id: str = ""
    last_run: str = ""
    last_status: str = ""            # ok | error | denied | running
    last_summary: str = ""

    def _at(self, day: date) -> datetime:
        h, m = map(int, self.time.split(":"))
        return datetime.combine(day, time(h, m))

    def _runs_on(self, day: date) -> bool:
        if self.date:
            return day.isoformat() == self.date
        return not self.days or day.weekday() in self.days

    def next_run(self, now: datetime) -> datetime | None:
        if not self.enabled:
            return None
        if self.date:
            at = self._at(date.fromisoformat(self.date))
            return at if at > now else None
        for k in range(0, 8):
            day = now.date() + timedelta(days=k)
            if self._runs_on(day) and self._at(day) > now:
                return self._at(day)
        return None

    def previous_run(self, now: datetime) -> datetime | None:
        for k in range(0, 8):
            day = now.date() - timedelta(days=k)
            if self._runs_on(day) and self._at(day) <= now:
                return self._at(day)
        return None

    def schedule(self, en: bool = False) -> str:
        if self.date:
            d = date.fromisoformat(self.date)
            return (f"once {d:%Y-%m-%d} {self.time}" if en else f"einmalig {d:%d.%m.%Y} {self.time}")
        return f"{days_label(self.days, en)} {self.time}"


def when_label(dt: datetime | None, now: datetime, en: bool = False) -> str:
    if dt is None:
        return "–"
    if dt.date() == now.date():
        return ("today " if en else "heute ") + dt.strftime("%H:%M")
    if dt.date() == now.date() + timedelta(days=1):
        return ("tomorrow " if en else "morgen ") + dt.strftime("%H:%M")
    names = DAY_NAMES_EN if en else DAY_NAMES
    return f"{names[dt.weekday()]} {dt:%d.%m.} {dt:%H:%M}"


class RoutineStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.items: list[Routine] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            known = {f.name for f in fields(Routine)}
            self.items = [Routine(**{k: v for k, v in r.items() if k in known})
                          for r in json.loads(self.path.read_text(encoding="utf-8"))]
        except (json.JSONDecodeError, TypeError, OSError):
            self.items = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(r) for r in self.items], ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _clean(name: str, task: str, at: str, days, day: str) -> dict:
        name, task = str(name).strip(), str(task).strip()
        if not task:
            raise ValueError("Bitte beschreiben, was die Routine tun soll.")
        once = ""
        if str(day or "").strip():
            try:
                once = date.fromisoformat(str(day).strip()).isoformat()
            except ValueError:
                from .dates import resolve
                when = resolve(str(day))
                if when is None:
                    raise ValueError(f"Datum '{day}' nicht verstanden (YYYY-MM-DD oder z. B. 'nächsten Freitag').") from None
                once = when.day.isoformat()
        return {"name": (name or task)[:60], "task": task, "time": parse_time(at),
                "days": [] if once else parse_days(days), "date": once}

    def add(self, name: str, task: str, at: str, days=None, day: str = "", now: datetime | None = None) -> Routine:
        data = self._clean(name, task, at, days, day)
        now = now or datetime.now()
        r = Routine(id=uuid.uuid4().hex[:6], created=now.isoformat(timespec="seconds"), **data)
        if r.date and r.next_run(now) is None:
            raise ValueError("Dieser Zeitpunkt liegt in der Vergangenheit.")
        with self._lock:
            self.items.append(r)
            self.save()
        return r

    def get(self, rid: str) -> Routine | None:
        return next((r for r in self.items if r.id == rid), None)

    def find(self, which: str) -> list[Routine]:
        w = str(which).strip().lower()
        return ([r for r in self.items if r.id == w] or [r for r in self.items if r.name.lower() == w]
                or [r for r in self.items if w and w in r.name.lower()])

    def update(self, rid: str, **changes) -> Routine:
        r = self.get(rid)
        if not r:
            raise KeyError(rid)
        merged = {"name": r.name, "task": r.task, "at": r.time, "days": r.days, "day": r.date}
        for key, target in (("name", "name"), ("task", "task"), ("time", "at"), ("days", "days"), ("date", "day")):
            if key in changes and changes[key] is not None:
                merged[target] = changes[key]
        data = self._clean(merged["name"], merged["task"], merged["at"], merged["days"], merged["day"])
        with self._lock:
            for k, v in data.items():
                setattr(r, k, v)
            if "enabled" in changes and changes["enabled"] is not None:
                r.enabled = bool(changes["enabled"])
            self.save()
        return r

    def delete(self, rid: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [r for r in self.items if r.id != rid]
            if len(self.items) != before:
                self.save()
                return True
        return False

    def due(self, now: datetime) -> list[Routine]:
        """Routinen, deren letzter Termin erreicht, aber noch nicht ausgeführt ist (höchstens CATCH_UP alt)."""
        out = []
        for r in self.items:
            if not r.enabled or r.last_status == "running":
                continue
            prev = r.previous_run(now)
            if prev is None or now - prev > CATCH_UP:
                continue
            since = max(filter(None, (r.last_run, r.created)), default="")
            if since and datetime.fromisoformat(since) >= prev:
                continue
            out.append(r)
        return out

    def mark_started(self, rid: str, now: datetime) -> None:
        with self._lock:
            r = self.get(rid)
            if r:
                r.last_run, r.last_status = now.isoformat(timespec="seconds"), "running"
                self.save()

    def set_result(self, rid: str, status: str, summary: str, chat_id: str = "") -> None:
        with self._lock:
            r = self.get(rid)
            if not r:
                return
            r.last_status, r.last_summary = status, summary[:500]
            if chat_id:
                r.chat_id = chat_id
            if r.date:
                r.enabled = False  # einmalige Routine ist erledigt
            self.save()

    def reset_running(self) -> None:
        """Nach einem Neustart: Läufe, die beim Beenden noch liefen, als abgebrochen markieren."""
        with self._lock:
            changed = False
            for r in self.items:
                if r.last_status == "running":
                    r.last_status, changed = "error", True
            if changed:
                self.save()

    def to_dict(self, r: Routine, now: datetime, en: bool = False) -> dict:
        return {**asdict(r), "schedule": r.schedule(en), "next": when_label(r.next_run(now), now, en),
                "next_iso": (r.next_run(now) or datetime.min).isoformat(timespec="seconds") if r.next_run(now) else ""}
