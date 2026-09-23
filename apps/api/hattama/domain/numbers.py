"""Russian and Kazakh number words -> int (cardinals and day ordinals used in deadlines)."""

from __future__ import annotations

import re

RU_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "одного": 1, "одной": 1, "два": 2, "две": 2, "двух": 2, "три": 3,
    "трех": 3, "трёх": 3, "четыре": 4, "четырех": 4, "четырёх": 4, "пять": 5, "пяти": 5, "шесть": 6, "шести": 6,
    "семь": 7, "семи": 7, "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10,
    "одиннадцать": 11, "одиннадцати": 11, "двенадцать": 12, "двенадцати": 12, "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14, "пятнадцать": 15, "пятнадцати": 15, "шестнадцать": 16,
    "шестнадцати": 16, "семнадцать": 17, "семнадцати": 17, "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19, "полтора": 1, "пару": 2, "пара": 2,
}
RU_TENS = {"двадцать": 20, "двадцати": 20, "тридцать": 30, "тридцати": 30, "сорок": 40, "пятьдесят": 50}
# ordinals in genitive/dative/accusative/nominative as used with dates: "к пятнадцатому", "до 15-го", "первого"
RU_ORDINAL_STEMS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8, "девят": 9,
    "десят": 10, "одиннадцат": 11, "двенадцат": 12, "тринадцат": 13, "четырнадцат": 14, "пятнадцат": 15,
    "шестнадцат": 16, "семнадцат": 17, "восемнадцат": 18, "девятнадцат": 19, "двадцат": 20, "тридцат": 30,
}
KK_UNITS = {
    "бір": 1, "екі": 2, "үш": 3, "төрт": 4, "бес": 5, "алты": 6, "жеті": 7, "сегіз": 8, "тоғыз": 9, "он": 10,
    "жиырма": 20, "отыз": 30,
}
_DIGITS = re.compile(r"^\d{1,4}$")


def parse_ru_cardinal(tokens: list[str]) -> tuple[int, int] | None:
    """Parse a cardinal at the start of tokens. Returns (value, tokens_consumed)."""
    if not tokens:
        return None
    t0 = tokens[0]
    if _DIGITS.match(t0):
        return int(t0), 1
    if t0 in RU_TENS:
        value = RU_TENS[t0]
        if len(tokens) > 1 and tokens[1] in RU_UNITS and RU_UNITS[tokens[1]] < 10:
            return value + RU_UNITS[tokens[1]], 2
        return value, 1
    if t0 in RU_UNITS:
        return RU_UNITS[t0], 1
    return None


def parse_ru_day_ordinal(tokens: list[str]) -> tuple[int, int] | None:
    """'пятнадцатого', 'двадцать пятого', '15-го', '15' -> day number."""
    if not tokens:
        return None
    t0 = tokens[0]
    m = re.match(r"^(\d{1,2})(?:-?(?:го|е|ое|ого|му|ому|м))?$", t0)
    if m:
        return int(m.group(1)), 1
    base = 0
    consumed = 0
    if t0 in RU_TENS and len(tokens) > 1:
        base, consumed = RU_TENS[t0], 1
        t0 = tokens[1]
    for stem, val in sorted(RU_ORDINAL_STEMS.items(), key=lambda kv: -len(kv[0])):
        if t0.startswith(stem) and len(t0) - len(stem) <= 4:
            if base and val >= 10:
                continue
            return base + val, consumed + 1
    return None


def parse_kk_number(tokens: list[str]) -> tuple[int, int] | None:
    """Kazakh numerals incl. ordinals/case endings: 'он бесінші', '15-і', 'екі', 'бес күн'."""
    if not tokens:
        return None
    m = re.match(r"^(\d{1,2})(?:-?(?:і|ы|сі|сы|ші|шы|інші|ыншы|ға|ге|қа|ке|на|не|ына|іне|сына|сіне))?$", tokens[0])
    if m:
        return int(m.group(1)), 1
    total, consumed = 0, 0
    for tok in tokens[:3]:
        matched = None
        for word, val in sorted(KK_UNITS.items(), key=lambda kv: -len(kv[0])):
            if word == "он":  # too short for prefix matching ("онда" = "there")
                if tok in ("он", "оныншы", "онға", "онына"):
                    matched = val
                    break
                continue
            if tok == word or (tok.startswith(word) and len(tok) - len(word) <= 5):
                matched = val
                break
        if matched is None:
            break
        if total and matched >= 10:
            break
        total += matched
        consumed += 1
        if matched < 10:
            break
    return (total, consumed) if consumed else None
