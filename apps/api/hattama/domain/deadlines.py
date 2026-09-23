"""Deadline normalisation policy `deadline-policy-v1` (docs/deadline-policy.md).

Deterministic, testable, language-aware (RU/KZ/mixed). The LLM only quotes the raw wording; dates are
computed here from the MEETING DATE (never from the system clock). Unknown year is not invented: the
year is taken from the meeting date and the assumption is shown in interpretation_notes.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta

from hattama.domain.numbers import RU_UNITS, parse_kk_number, parse_ru_cardinal, parse_ru_day_ordinal

POLICY_VERSION = "deadline-policy-v1"
KINDS = ("date", "relative", "range", "event", "missing", "ambiguous")

RU_MONTHS = [("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("май", 5), ("мая", 5), ("мае", 5),
             ("июн", 6), ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11),
             ("декабр", 12)]
KK_MONTHS = [("қаңтар", 1), ("ақпан", 2), ("наурыз", 3), ("сәуір", 4), ("мамыр", 5), ("маусым", 6), ("шілде", 7),
             ("тамыз", 8), ("қыркүйек", 9), ("қазан", 10), ("қараша", 11), ("желтоқсан", 12)]
RU_WEEKDAYS = [("понедельник", 0), ("вторник", 1), ("сред", 2), ("четверг", 3), ("пятниц", 4), ("суббот", 5),
               ("воскресен", 6)]
KK_WEEKDAYS = [("дүйсенбі", 0), ("сейсенбі", 1), ("сәрсенбі", 2), ("бейсенбі", 3), ("жұма", 4), ("сенбі", 5),
               ("жексенбі", 6)]
WEEKDAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

RU_UNIT_WORDS = [("рабоч", "business_day"), ("дн", "day"), ("ден", "day"), ("сут", "day"), ("недел", "week"),
                 ("месяц", "month"), ("месяч", "month")]
KK_UNIT_WORDS = [("жұмыс", "business_day"), ("күн", "day"), ("апта", "week"), ("ай", "month")]

MISSING_PATTERNS = ("срок не указан", "без срока", "срок не определ", "мерзімі жоқ", "мерзім көрсетілмеген",
                    "не установлен")
VAGUE_PATTERNS = ("как можно скорее", "срочно", "в ближайшее время", "оперативно", "побыстрее", "позже", "потом",
                  "когда будет возможность", "жақын арада", "тезірек", "мүмкіндігінше тез", "кейінірек", "asap")
EVENT_MARKERS_RU = ("после ", "по итогам", "по результатам", "по завершении", "по готовности", "по окончании",
                    "по получении", "после того как", "как только", "к следующему совещанию",
                    "к следующей планерке", "к следующей планёрке", "к следующей встрече")
EVENT_MARKERS_KK = ("кейін", "соң", "қорытындысы бойынша", "нәтижесі бойынша", "дайын болғанда", "келесі жиналысқа",
                    "келесі кездесуге")
TIME_NOUNS = ("совещани", "встреч", "планерк", "планёрк", "работ", "поставк", "получени", "согласовани", "подписани",
              "проверк", "жиналыс", "кездесу", "жұмыс", "аяқтал", "алған", "тексеру")


@dataclass
class Deadline:
    raw_text: str | None
    kind: str
    normalized_date: date | None = None
    range_start: date | None = None
    range_end: date | None = None
    timezone: str = "Asia/Almaty"
    anchor_date: date | None = None
    anchor_event: str | None = None
    offset: dict[str, Any] | None = None
    year_inferred: bool = False
    ambiguous: bool = False
    conflict: bool = False
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    interpretation_notes: list[str] = field(default_factory=list)
    policy_version: str = POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key in ("normalized_date", "range_start", "range_end", "anchor_date"):
            if out[key] is not None:
                out[key] = out[key].isoformat()
        for alt in out["alternatives"]:
            if isinstance(alt.get("date"), date):
                alt["date"] = alt["date"].isoformat()
        return out

    @property
    def needs_review(self) -> bool:
        return self.ambiguous or self.conflict or self.kind == "ambiguous"


@dataclass
class _Cand:
    kind: str
    value: date
    desc: str
    pos: int
    range_start: date | None = None
    year_inferred: bool = False
    offset: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    ambiguous: bool = False


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w]+(?:-[\w]+)?", text.lower().replace("ё", "е"))


def add_business_days(start: date, n: int, holidays: frozenset[date] = frozenset()) -> date:
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in holidays:
            added += 1
    return d


def _week_friday(d: date) -> date:
    return d + timedelta(days=4 - d.weekday()) if d.weekday() <= 4 else d + timedelta(days=11 - d.weekday())


def _match_stem(tok: str, table: list[tuple[str, int]]) -> int | None:
    for stem, val in sorted(table, key=lambda kv: -len(kv[0])):
        if tok.startswith(stem):
            return val
    return None


def _unit(tok: str) -> str | None:
    for stem, unit in RU_UNIT_WORDS + KK_UNIT_WORDS:
        if tok.startswith(stem):
            if stem == "ай" and tok not in ("ай", "айға", "айда", "айдың", "айы", "айлық", "ай-ақ"):
                continue
            if stem == "дн" and not re.match(r"^дн(я|ей|ям|и)$", tok):
                continue
            return unit
    return None


def _shift(anchor: date, value: int, unit: str, holidays: frozenset[date]) -> date:
    if unit == "day":
        return anchor + timedelta(days=value)
    if unit == "business_day":
        return add_business_days(anchor, value, holidays)
    if unit == "week":
        return anchor + timedelta(weeks=value)
    return anchor + relativedelta(months=value)


def _explicit_dates(toks: list[str], text: str, meeting: date) -> list[_Cand]:
    cands: list[_Cand] = []
    for m in re.finditer(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", text):
        day, month = int(m.group(1)), int(m.group(2))
        year = m.group(3)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        y = (2000 + int(year) if year and len(year) == 2 else int(year)) if year else None
        c = _mk_date(day, month, y, meeting, m.start())
        if c:
            cands.append(c)
    for i, tok in enumerate(toks):
        month = _match_stem(tok, RU_MONTHS) if not tok.isdigit() else None
        kk_month = _match_stem(tok, KK_MONTHS) if month is None and not tok.isdigit() else None
        if month is None and kk_month is None:
            continue
        mon = month or kk_month
        day = None
        # RU: "<day> <month>" | KK: "<day> <month>" or "<month>(ның) <day>"
        for back in (2, 1):
            if i - back >= 0:
                parsed = parse_ru_day_ordinal(toks[i - back:i]) or parse_kk_number(toks[i - back:i])
                if parsed and parsed[1] == back and 1 <= parsed[0] <= 31:
                    day = parsed[0]
                    break
        if day is None and kk_month and i + 1 < len(toks):
            parsed = parse_kk_number(toks[i + 1:i + 3])
            if parsed and 1 <= parsed[0] <= 31:
                day = parsed[0]
        if day is None:
            continue
        year = None
        if i + 1 < len(toks) and re.match(r"^20\d\d$", toks[i + 1]):
            year = int(toks[i + 1])
        c = _mk_date(day, mon, year, meeting, text.find(tok))  # type: ignore[arg-type]
        if c:
            cands.append(c)
    return cands


def _mk_date(day: int, month: int, year: int | None, meeting: date, pos: int) -> _Cand | None:
    notes: list[str] = []
    inferred = year is None
    y = year or meeting.year
    try:
        d = date(y, month, day)
    except ValueError:
        return None
    ambiguous = False
    if inferred:
        notes.append(f"год не назван: принят {y} по дате встречи")
        if d < meeting:
            try:
                d = date(y + 1, month, day)
            except ValueError:
                return None
            notes[-1] = f"год не назван; дата уже прошла на момент встречи — принят {y + 1}"
            ambiguous = (d - meeting).days > 183
        elif (d - meeting).days > 183:
            ambiguous = True
            notes.append("дата более чем через полгода от встречи — проверьте год")
    return _Cand("date", d, f"{day:02d}.{month:02d}", pos, year_inferred=inferred, notes=notes, ambiguous=ambiguous)


def _durations(toks: list[str], meeting: date, holidays: frozenset[date]) -> list[_Cand]:
    """'за две недели', 'в течение 5 рабочих дней', 'через месяц', 'бір апта ішінде', 'три недели'."""
    cands: list[_Cand] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        num = parse_ru_cardinal(toks[i:i + 2]) or parse_kk_number(toks[i:i + 3])
        if num is not None:
            value, used = num
            j = i + used
            unit = _unit(toks[j]) if j < len(toks) else None
            if unit == "business_day" and j + 1 < len(toks) and _unit(toks[j + 1]) == "day":
                j += 1
            if unit:
                if toks[i] == "полтора" and unit in ("week", "month"):
                    d = meeting + (timedelta(days=10) if unit == "week" else relativedelta(days=45))
                    cands.append(_Cand("relative", d, f"полтора {unit}", i, offset={"value": 1.5, "unit": unit},
                                       notes=["«полтора» округлено вверх по политике"]))
                else:
                    d = _shift(meeting, value, unit, holidays)
                    cands.append(_Cand("relative", d, f"{value} {unit}", i, offset={"value": value, "unit": unit}))
                i = j + 1
                continue
        unit = _unit(tok)
        prev = toks[i - 1] if i else ""
        if unit in ("week", "month", "day") and prev in ("за", "через", "течение", "в", "на") and \
                not (i + 1 < len(toks) and toks[i + 1] in ("назад",)) and tok not in ("дней", "дня", "дням"):
            # "за неделю", "через месяц", "в течение недели", "за день"
            d = _shift(meeting, 1, unit, holidays)
            cands.append(_Cand("relative", d, f"1 {unit}", i, offset={"value": 1, "unit": unit}))
        elif unit in ("week", "month") and tok in ("апта", "ай") and i + 1 < len(toks) and \
                toks[i + 1].startswith("ішін"):
            d = _shift(meeting, 1, unit, holidays)
            cands.append(_Cand("relative", d, f"1 {unit}", i, offset={"value": 1, "unit": unit}))
        i += 1
    return cands


def _named_periods(text: str, toks: list[str], meeting: date) -> list[_Cand]:
    c: list[_Cand] = []
    t = text.lower().replace("ё", "е")

    def add(kind: str, d: date, desc: str, pos: int, notes: list[str], amb: bool = False,
            start: date | None = None) -> None:
        c.append(_Cand(kind, d, desc, pos, range_start=start, notes=notes, ambiguous=amb))

    fri = _week_friday(meeting)
    weekend_note = ["встреча в выходной: «конец недели» отнесён к следующей рабочей неделе"] \
        if meeting.weekday() >= 5 else []
    for pat in ("до конца недели", "к концу недели", "до конца этой недели", "к концу этой недели", "на этой неделе",
                "апта соңына дейін", "осы аптаның соңына", "осы аптада", "апта аяғына дейін"):
        if (p := t.find(pat)) >= 0:
            add("relative", fri, "конец рабочей недели", p,
                ["«конец недели» = пятница рабочей недели встречи (политика)", *weekend_note], bool(weekend_note))
    for pat in ("до конца следующей недели", "к концу следующей недели", "келесі аптаның соңына"):
        if (p := t.find(pat)) >= 0:
            c[:] = [x for x in c if x.desc != "конец рабочей недели"]
            add("relative", _week_friday(meeting + timedelta(days=7 - meeting.weekday())), "конец следующей недели",
                p, ["= пятница следующей недели"])
    for pat in ("на следующей неделе", "келесі аптада", "в течение следующей недели"):
        if (p := t.find(pat)) >= 0:
            mon = meeting + timedelta(days=7 - meeting.weekday())
            add("range", mon + timedelta(days=4), "следующая неделя", p,
                ["диапазон пн–пт следующей недели; для напоминаний используется пятница"], start=mon)
    for pat in ("до следующей недели", "к следующей неделе", "келесі аптаға дейін"):
        if (p := t.find(pat)) >= 0:
            add("relative", fri, "до начала следующей недели", p,
                ["«до следующей недели» = последний рабочий день текущей недели; возможно — понедельник "
                 "следующей недели"], True)
    for pat in ("до конца месяца", "к концу месяца", "ай соңына дейін", "айдың соңына дейін", "осы айдың соңына"):
        if (p := t.find(pat)) >= 0:
            last = date(meeting.year, meeting.month, calendar.monthrange(meeting.year, meeting.month)[1])
            add("relative", last, "конец месяца", p, ["= последний календарный день месяца встречи"])
    for pat in ("до конца квартала", "к концу квартала", "тоқсан соңына дейін"):
        if (p := t.find(pat)) >= 0:
            q_end_month = ((meeting.month - 1) // 3 + 1) * 3
            add("relative", date(meeting.year, q_end_month, calendar.monthrange(meeting.year, q_end_month)[1]),
                "конец квартала", p, [])
    for pat in ("до конца года", "к концу года", "жыл соңына дейін"):
        if (p := t.find(pat)) >= 0:
            add("relative", date(meeting.year, 12, 31), "конец года", p, [])
    simple = [("послезавтра", 2), ("арғы күні", 2), ("завтра", 1), ("ертең", 1), ("сегодня", 0), ("бүгін", 0)]
    for pat, days in simple:
        if (p := t.find(pat)) >= 0 and not any(x.pos == p for x in c):
            if pat == "завтра" and "послезавтра" in t:
                continue
            add("relative", meeting + timedelta(days=days), pat, p, [])
    # weekdays
    for i, tok in enumerate(toks):
        wd = _match_stem(tok, RU_WEEKDAYS)
        if wd is None:
            wd = _match_stem(tok, KK_WEEKDAYS)
            if wd == 5 and tok.startswith(("дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жексенбі")):
                continue
        if wd is None:
            continue
        window = " ".join(toks[max(0, i - 3):i])
        next_week = any(w in window for w in ("следующ", "будущ", "келесі"))
        delta = (wd - meeting.weekday()) % 7
        notes = []
        amb = False
        if next_week:
            target = meeting + timedelta(days=7 - meeting.weekday() + wd)
            notes.append(f"{WEEKDAY_NAMES[wd]} следующей недели")
        else:
            if delta == 0:
                delta = 7
                amb = True
                notes.append(f"встреча сама в {WEEKDAY_NAMES[wd]}: принят следующий {WEEKDAY_NAMES[wd]}")
            target = meeting + timedelta(days=delta)
            notes.append(f"ближайший {WEEKDAY_NAMES[wd]} после даты встречи")
        before = toks[i - 1] if i else ""
        if before == "до":
            notes.append("«до» трактуется включительно (срок — сам день)")
        add("relative", target, WEEKDAY_NAMES[wd], text.lower().find(tok), notes, amb)
    return c


def _event_anchor(text: str) -> str | None:
    t = text.lower().replace("ё", "е")
    for marker in EVENT_MARKERS_RU:
        p = t.find(marker)
        if p >= 0:
            tail = t[p:].strip()
            if marker.startswith("к следующ"):
                return tail
            if any(noun in tail for noun in TIME_NOUNS) or marker in ("после того как", "как только",
                                                                       "по готовности"):
                return tail
    for marker in EVENT_MARKERS_KK:
        p = t.find(marker)
        if p >= 0:
            head = t[:p + len(marker)].strip()
            if any(noun in head for noun in TIME_NOUNS) or marker in ("дайын болғанда", "келесі жиналысқа",
                                                                    "келесі кездесуге"):
                return head
    return None


def normalize_deadline(raw_text: str | None, meeting_date: date, timezone: str = "Asia/Almaty",
                       holidays: frozenset[date] = frozenset()) -> Deadline:
    base = {"timezone": timezone, "anchor_date": meeting_date}
    if raw_text is None or not raw_text.strip():
        return Deadline(raw_text=None, kind="missing", interpretation_notes=["срок не назван"], **base)
    text = raw_text.strip()
    lowered = text.lower().replace("ё", "е")
    if any(p in lowered for p in MISSING_PATTERNS):
        return Deadline(raw_text=text, kind="missing", interpretation_notes=["в обсуждении прямо сказано, что срок "
                                                                            "не установлен"], **base)
    toks = _tokens(text)
    explicit = _explicit_dates(toks, lowered, meeting_date)
    anchor = _event_anchor(text)
    durations = _durations(toks, meeting_date, holidays)
    periods = _named_periods(lowered, toks, meeting_date)

    if anchor and not explicit:
        offset = durations[0].offset if durations else None
        notes = [f"срок зависит от события «{anchor}»; календарная дата неизвестна"]
        if offset:
            notes.append(f"смещение {offset['value']} {offset['unit']} отсчитывается от события, а не от встречи")
        return Deadline(raw_text=text, kind="event", anchor_event=anchor, offset=offset, interpretation_notes=notes,
                        **base)
    candidates = explicit + periods + durations
    if not candidates:
        if any(p in lowered for p in VAGUE_PATTERNS):
            return Deadline(raw_text=text, kind="ambiguous", ambiguous=True,
                            interpretation_notes=["неопределённая формулировка срока — дата не назначается"], **base)
        return Deadline(raw_text=text, kind="ambiguous", ambiguous=True,
                        interpretation_notes=["формулировку не удалось однозначно интерпретировать"], **base)
    primary = explicit[0] if explicit else sorted(candidates, key=lambda c: c.pos)[0]
    others = [c for c in candidates if c is not primary]
    conflict = False
    alternatives = []
    notes = list(primary.notes)
    for other in others:
        if abs((other.value - primary.value).days) > 1:
            conflict = True
            alternatives.append({"kind": other.kind, "date": other.value, "basis": other.desc})
        elif other.kind != primary.kind:
            notes.append(f"формулировки согласуются: «{other.desc}» ≈ {primary.value.isoformat()}")
    if conflict:
        notes.append("в формулировке срока есть противоречие: сохранены оба основания, нужна проверка секретаря")
    return Deadline(
        raw_text=text, kind=primary.kind, normalized_date=primary.value,
        range_start=primary.range_start, range_end=primary.value if primary.kind == "range" else None,
        offset=primary.offset, year_inferred=primary.year_inferred,
        ambiguous=primary.ambiguous or conflict, conflict=conflict, alternatives=alternatives,
        interpretation_notes=notes, **base)


def deadline_from_dict(data: dict[str, Any] | None) -> dict[str, Any]:
    return data or {"kind": "missing", "raw_text": None, "normalized_date": None, "policy_version": POLICY_VERSION}


__all__ = ["KINDS", "POLICY_VERSION", "RU_UNITS", "Deadline", "add_business_days", "normalize_deadline"]
