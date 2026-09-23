from __future__ import annotations

from datetime import date

import pytest

from hattama.domain.deadlines import add_business_days, normalize_deadline

THU = date(2026, 10, 1)  # Thursday
WED = date(2026, 9, 30)


def nd(raw, d=THU):  # type: ignore[no-untyped-def]
    return normalize_deadline(raw, d)


@pytest.mark.parametrize(
    ("raw", "expected", "kind"),
    [
        ("до конца недели", date(2026, 10, 2), "relative"),
        ("за две недели", date(2026, 10, 15), "relative"),
        ("за неделю", date(2026, 10, 8), "relative"),
        ("за месяц", date(2026, 11, 1), "relative"),
        ("в течение месяца", date(2026, 11, 1), "relative"),
        ("через 3 дня", date(2026, 10, 4), "relative"),
        ("завтра", date(2026, 10, 2), "relative"),
        ("к среде", date(2026, 10, 7), "relative"),
        ("до пятницы", date(2026, 10, 2), "relative"),
        ("к следующему понедельнику", date(2026, 10, 5), "relative"),
        ("до конца месяца", date(2026, 10, 31), "relative"),
        ("до 15.10", date(2026, 10, 15), "date"),
        ("к пятнадцатому октября", date(2026, 10, 15), "date"),
        ("до 20 ноября 2026", date(2026, 11, 20), "date"),
        ("в течение 5 рабочих дней", date(2026, 10, 8), "relative"),
        # Kazakh
        ("жұмаға дейін", date(2026, 10, 2), "relative"),
        ("бір апта ішінде", date(2026, 10, 8), "relative"),
        ("екі апта ішінде", date(2026, 10, 15), "relative"),
        ("15 қазанға дейін", date(2026, 10, 15), "date"),
        ("қазанның 20-сына дейін", date(2026, 10, 20), "date"),
        ("ертең", date(2026, 10, 2), "relative"),
        ("сәрсенбіге дейін", date(2026, 10, 7), "relative"),
        ("ай соңына дейін", date(2026, 10, 31), "relative"),
    ],
)
def test_normalization_table(raw: str, expected: date, kind: str) -> None:
    d = nd(raw)
    assert (d.normalized_date, d.kind) == (expected, kind), d.to_dict()


def test_scenario_a_consistent_relative_and_calendar() -> None:
    d = normalize_deadline("Три недели, то есть к пятнадцатому октября", date(2026, 9, 24))
    assert d.normalized_date == date(2026, 10, 15)
    assert not d.conflict
    assert any("согласуются" in n for n in d.interpretation_notes)


def test_scenario_a_contradiction_keeps_both_bases_and_needs_review() -> None:
    d = normalize_deadline("Три недели, то есть к пятнадцатому октября", date(2026, 9, 21))
    assert d.kind == "date" and d.normalized_date == date(2026, 10, 15)
    assert d.conflict and d.needs_review
    assert d.alternatives[0]["date"] == date(2026, 10, 12)


def test_scenario_d_event_dependency_has_no_invented_date() -> None:
    d = nd("по итогам совещания с подрядчиками")
    assert d.kind == "event" and d.normalized_date is None
    assert "совещания с подрядчиками" in (d.anchor_event or "")


def test_scenario_e_contract_term_is_event_relative_not_calendar() -> None:
    d = nd("в течение пяти рабочих дней после выполнения работ")
    assert d.kind == "event" and d.normalized_date is None
    assert d.offset == {"value": 5, "unit": "business_day"}


def test_missing_and_vague() -> None:
    assert nd(None).kind == "missing"
    assert nd("").kind == "missing"
    assert nd("срок не указан").kind == "missing"
    v = nd("как можно скорее")
    assert v.kind == "ambiguous" and v.normalized_date is None and v.needs_review
    assert nd("жақын арада").kind == "ambiguous"


def test_same_weekday_is_flagged() -> None:
    d = normalize_deadline("к среде", WED)
    assert d.normalized_date == date(2026, 10, 7) and d.ambiguous


def test_until_next_week_is_ambiguous() -> None:
    d = nd("келесі аптаға дейін")
    assert d.normalized_date == date(2026, 10, 2) and d.ambiguous
    d = nd("до следующей недели")
    assert d.ambiguous


def test_next_week_is_range() -> None:
    d = nd("на следующей неделе")
    assert d.kind == "range" and d.range_start == date(2026, 10, 5) and d.normalized_date == date(2026, 10, 9)


def test_year_not_invented_but_inferred_with_visible_note() -> None:
    d = normalize_deadline("к 15 января", date(2026, 12, 10))
    assert d.normalized_date == date(2027, 1, 15) and d.year_inferred
    assert any("год не назван" in n for n in d.interpretation_notes)
    explicit = nd("15.10.2027")
    assert not explicit.year_inferred and explicit.normalized_date == date(2027, 10, 15)


def test_meeting_date_not_system_clock() -> None:
    assert normalize_deadline("завтра", date(2020, 2, 28)).normalized_date == date(2020, 2, 29)


def test_business_days_skip_weekends() -> None:
    assert add_business_days(date(2026, 10, 2), 1) == date(2026, 10, 5)
    assert add_business_days(date(2026, 10, 2), 5, frozenset({date(2026, 10, 6)})) == date(2026, 10, 12)


def test_serialization_is_json_ready() -> None:
    data = nd("за две недели").to_dict()
    assert data["normalized_date"] == "2026-10-15" and data["anchor_date"] == "2026-10-01"
    assert data["policy_version"] == "deadline-policy-v1"
