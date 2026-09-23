"""Matching spoken names to meeting participants across Russian and Kazakh inflections.

A match is a SUGGESTION with a reason; identity is never inferred from voice timbre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KK_TO_RU = str.maketrans({"ә": "а", "ө": "о", "ү": "у", "ұ": "у", "қ": "к", "ғ": "г", "ң": "н", "һ": "х",
                          "і": "и", "ё": "е"})
# longest first; RU declension + KZ case/possessive endings
ENDINGS = sorted({
    "ами", "ями", "ому", "ему", "ого", "его", "ой", "ей", "ом", "ем", "ым", "им", "ах", "ях", "а", "я", "у", "ю",
    "е", "ы", "и", "ов", "ев",
    "нын", "нин", "дын", "дин", "тын", "тин", "мен", "бен", "пен", "дан", "ден", "тан", "тен", "нан", "нен",
    "га", "ге", "ка", "ке", "на", "не", "ды", "ди", "ты", "ти", "ны", "ни", "да", "де", "та", "те", "ке", "ка",
    "ына", "ине", "сына", "сине",
}, key=len, reverse=True)
DEPARTMENT_WORDS = ("департамент", "отдел", "управлени", "служб", "бухгалтери", "юридическ", "юрист", "дирекци",
                    "бөлім", "басқарма", "қызмет", "департаменті", "комитет", "комисси", "группа", "штаб")
GROUP_WORDS = ("все", "всем", "каждый", "мы", "барлығы", "бәріміз", "бәрі", "команда")


def norm(text: str) -> str:
    text = text.lower().translate(KK_TO_RU)
    return re.sub(r"[^\w\s-]", "", text).strip()


def stem(word: str) -> str:
    w = norm(word)
    for end in ENDINGS:
        if len(w) - len(end) >= 3 and w.endswith(end):
            return w[: -len(end)]
    return w


def assignee_kind(name: str) -> str:
    n = name.lower()
    if any(w in n for w in DEPARTMENT_WORDS):
        return "department"
    if n.strip() in GROUP_WORDS:
        return "group"
    return "person"


@dataclass(frozen=True)
class ParticipantRef:
    id: str
    display_name: str
    aliases: tuple[str, ...]
    is_present: bool
    kind: str = "person"
    user_id: str | None = None


@dataclass(frozen=True)
class NameMatch:
    participant: ParticipantRef
    score: float
    reason: str


def match_participant(name: str, participants: list[ParticipantRef]) -> NameMatch | None:
    if not name or not name.strip():
        return None
    target_tokens = [stem(t) for t in norm(name).split() if t]
    if not target_tokens:
        return None
    best: NameMatch | None = None
    for p in participants:
        variants = [p.display_name, *p.aliases]
        for variant in variants:
            v_tokens = [stem(t) for t in norm(variant).split() if t]
            if not v_tokens:
                continue
            if norm(variant) == norm(name):
                return NameMatch(p, 1.0, "точное совпадение имени")
            common = set(target_tokens) & set(v_tokens)
            if common:
                score = 0.9 if len(common) == len(target_tokens) else 0.75
                if best is None or score > best.score:
                    best = NameMatch(p, score, f"совпадение основы «{sorted(common)[0]}» с учётом падежа")
                continue
            for t in target_tokens:
                for v in v_tokens:
                    if len(t) >= 4 and len(v) >= 4 and (t.startswith(v) or v.startswith(t)) and abs(len(t) - len(v)) <= 2:
                        if best is None or best.score < 0.6:
                            best = NameMatch(p, 0.6, f"частичное совпадение «{t}»/«{v}»")
    return best


def addressee_of(text: str) -> str | None:
    """Vocative at the beginning of an utterance: 'Ботагоз, …' / 'Ерлан, …' -> the addressed name."""
    m = re.match(r"^\s*([А-ЯЁӘҒҚҢӨҰҮҺІ][а-яёәғқңөұүһі-]{2,}(?:\s+[А-ЯЁӘҒҚҢӨҰҮҺІ][а-яёәғқңөұүһі-]{2,})?)\s*,", text)
    return m.group(1) if m else None
