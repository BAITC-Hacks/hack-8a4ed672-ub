"""Deterministic linking rules on the case situations A–G.

Events here are SCRIPTED (what a correct reader of the dialogue would extract) — this checks the
linking/normalisation policy, NOT the LLM. LLM quality is measured by `tasks.py evaluate`.
"""

from __future__ import annotations

from datetime import date

from hattama.domain.names import ParticipantRef
from hattama.extraction.evidence import SegmentView
from hattama.extraction.linker import LinkContext, Linker
from hattama.extraction.schemas import LlmEvent

MEETING = date(2026, 9, 24)  # Thursday


def ctx(lines: list[tuple[str | None, str]], participants: list[ParticipantRef]) -> LinkContext:
    segs = {}
    ordered = []
    for i, (speaker, text) in enumerate(lines, 1):
        v = SegmentView(id=f"seg{i}", alias=f"S{i}", text=text, start_ms=i * 10000, end_ms=i * 10000 + 8000,
                        revision_id="rev1", speaker_name=speaker, order=i - 1)
        segs[v.alias] = v
        ordered.append(v)
    return LinkContext(MEETING, "Asia/Almaty", participants, segs, ordered)


def P(name: str, present: bool = True, pid: str | None = None) -> ParticipantRef:  # noqa: N802
    return ParticipantRef(pid or f"p-{name}", name, (), present)


def run(c: LinkContext, events: list[dict]) -> list[dict]:
    linker = Linker(c)
    linker.begin_window()
    for i, e in enumerate(events):
        linker.apply(LlmEvent(**e), i)
    return linker.cards()


def by_action(cards: list[dict], word: str) -> dict:
    found = [c for c in cards if word in c["action"]]
    assert len(found) == 1, [c["action"] for c in cards]
    return found[0]


def test_a_rejected_two_weeks_are_not_the_result() -> None:
    c = ctx([
        ("Руководитель", "Айгерим, нужно переаттестовать рабочие места на втором участке. Две недели достаточно?"),
        ("Айгерим", "Нет, за две недели не успеем, там одиннадцать площадок. Нужно три недели, то есть к "
                    "пятнадцатому октября."),
        ("Руководитель", "Хорошо, три недели, до пятнадцатого октября. А смету за неделю дадите?"),
        ("Айгерим", "Да, смету дам за неделю."),
    ], [P("Руководитель"), P("Айгерим")])
    cards = run(c, [
        {"type": "assign", "segment": "S1", "task": "N1", "action": "переаттестовать рабочие места на втором участке",
         "action_quote": "переаттестовать рабочие места на втором участке", "assignee": "Айгерим",
         "assignee_quote": "Айгерим"},
        {"type": "propose", "segment": "S1", "task": "N1", "deadline_quote": "Две недели"},
        {"type": "reject", "segment": "S2", "task": "N1", "deadline_quote": "три недели, то есть к пятнадцатому октября"},
        {"type": "accept", "segment": "S3", "task": "N1", "deadline_quote": "три недели, до пятнадцатого октября"},
        {"type": "propose", "segment": "S3", "task": "N2", "action": "подготовить смету", "action_quote": "смету",
         "assignee": "Айгерим", "deadline_quote": "за неделю", "parent": "N1"},
        {"type": "accept", "segment": "S4", "task": "N2", "action_quote": "смету дам за неделю"},
    ])
    main = by_action(cards, "переаттест")
    assert main["deadline"]["normalized_date"] == "2026-10-15"
    assert main["deadline"]["kind"] == "date"
    assert any(h["event"] == "proposal_rejected" for h in main["history"])
    assert "две недели" not in (main["deadline"]["raw_text"] or "").lower().split(",")[0]
    sub = by_action(cards, "смет")
    assert sub["parent_ref"] == main["ref"]
    assert sub["deadline"]["normalized_date"] == "2026-10-01"
    assert sub["agreement"] == "agreed"


def test_a_contradiction_between_relative_and_calendar_keeps_both() -> None:
    c = ctx([("Руководитель", "Нужно три недели, то есть к пятнадцатому октября."),
             ("Руководитель", "Айгерим, это на вас.")], [P("Айгерим")])
    c.meeting_date = date(2026, 9, 21)
    cards = run(c, [{"type": "assign", "segment": "S1", "task": "N1", "action": "переаттестовать рабочие места",
                     "assignee": "Айгерим", "deadline_quote": "три недели, то есть к пятнадцатому октября"}])
    card = cards[0]
    assert card["deadline"]["conflict"] and card["deadline"]["normalized_date"] == "2026-10-15"
    assert card["deadline"]["alternatives"][0]["date"] == "2026-10-12"
    assert card["review_state"] == "needs_review"
    assert any(i["kind"] == "deadline_conflict" for i in card["issues"])


LINES_B = [
    ("Руководитель", "Ботагоз, по поставкам кабеля ситуация критическая. Пусть Ерлан до конца недели подготовит "
                     "претензию, а вы за две недели найдите альтернативного поставщика."),
    ("Ботагоз", "Хорошо, две недели мне хватит. Претензию Ерлан подготовит, я с ним свяжусь сегодня."),
    ("Руководитель", "Договор пока не расторгаем. Если поставщик систематически нарушает сроки, тогда расторгаем."),
]
EVENTS_B = [
    {"type": "assign", "segment": "S1", "task": "N1", "action": "подготовить претензию поставщику",
     "action_quote": "подготовит претензию", "assignee": "Ерлан", "assignee_quote": "Пусть Ерлан",
     "deadline_quote": "до конца недели"},
    {"type": "assign", "segment": "S1", "task": "N2", "action": "найти альтернативного поставщика",
     "action_quote": "найдите альтернативного поставщика", "assignee": "Ботагоз", "assignee_quote": "Ботагоз",
     "deadline_quote": "за две недели"},
    {"type": "accept", "segment": "S2", "task": "N2", "action_quote": "две недели мне хватит"},
    {"type": "conditional", "segment": "S3", "task": "N3", "action": "расторгнуть договор с поставщиком",
     "action_quote": "тогда расторгаем", "condition_quote": "Если поставщик систематически нарушает сроки"},
]


def test_b_two_assignments_different_assignees_and_deadlines() -> None:
    c = ctx(LINES_B, [P("Руководитель"), P("Ботагоз"), P("Ерлан", present=False)])
    cards = run(c, EVENTS_B)
    claim = by_action(cards, "претензи")
    supplier = by_action(cards, "поставщика")
    assert claim["assignee"]["name"] == "Ерлан" and claim["assignee_type"] == "mentioned_person"
    assert claim["deadline"]["normalized_date"] == "2026-09-25"
    assert supplier["assignee"]["name"] == "Ботагоз" and supplier["assignee_type"] == "participant"
    assert supplier["deadline"]["normalized_date"] == "2026-10-08"
    # «Ботагоз, пусть Ерлан …» — curator/assignee ambiguity is visible, not silently resolved
    assert any("обращена к «Ботагоз»" in r for r in claim["review_reasons"])
    assert claim["assigner"]["name"] == "Руководитель"


def test_b_later_summary_does_not_silently_erase_erlan() -> None:
    lines = [*LINES_B, ("Ботагоз", "Итак, на мне претензия и поставщик.")]
    c = ctx(lines, [P("Руководитель"), P("Ботагоз"), P("Ерлан", present=False)])
    cards = run(c, [*EVENTS_B,
                    {"type": "restate", "segment": "S4", "task": "T1", "assignee": "Ботагоз",
                     "action_quote": "на мне претензия"},
                    {"type": "restate", "segment": "S4", "task": "T2", "assignee": "Ботагоз"}])
    claim = by_action(cards, "претензи")
    assert claim["assignee"]["name"] == "Ерлан"
    assert any(i["kind"] == "restatement_conflict" and i["field"] == "assignee" for i in claim["issues"])
    assert claim["review_state"] == "needs_review"
    supplier = by_action(cards, "поставщика")
    assert not supplier["issues"]


def test_f_conditional_decision_stays_conditional() -> None:
    c = ctx(LINES_B, [P("Руководитель"), P("Ботагоз")])
    cards = run(c, EVENTS_B)
    terminate = by_action(cards, "расторгнуть")
    assert terminate["conditional"] is True
    assert "систематически" in terminate["condition"]
    assert terminate["execution_state"] == "open" and terminate["deadline"]["kind"] == "missing"


def test_c_intermediate_estimate_and_final_month() -> None:
    c = ctx([("Руководитель", "Нурлан, очередь по переаттестации надо закрыть за месяц."),
             ("Нурлан", "Понял. За неделю дам смету, потом начнём.")], [P("Руководитель"), P("Нурлан")])
    cards = run(c, [
        {"type": "assign", "segment": "S1", "task": "N1", "action": "закрыть очередь по переаттестации",
         "action_quote": "очередь по переаттестации надо закрыть", "assignee": "Нурлан", "deadline_quote": "за месяц"},
        {"type": "assign", "segment": "S2", "task": "N2", "action": "подготовить смету", "action_quote": "дам смету",
         "assignee": "Нурлан", "deadline_quote": "За неделю", "parent": "T1"},
    ])
    final = by_action(cards, "очередь")
    est = by_action(cards, "смет")
    assert final["deadline"]["normalized_date"] == "2026-10-24"  # month not lost
    assert est["deadline"]["normalized_date"] == "2026-10-01" and est["parent_ref"] == final["ref"]


def test_d_event_dependent_deadline_has_no_date() -> None:
    c = ctx([("Руководитель", "Салтанат, справку подготовьте по итогам совещания с подрядчиками.")],
            [P("Салтанат")])
    card = run(c, [{"type": "assign", "segment": "S1", "task": "N1", "action": "подготовить справку",
                    "action_quote": "справку подготовьте", "assignee": "Салтанат",
                    "deadline_quote": "по итогам совещания с подрядчиками"}])[0]
    assert card["deadline"]["kind"] == "event" and card["deadline"]["normalized_date"] is None
    assert card["dependencies"] and "подрядчиками" in card["dependencies"][0]["event"]


def test_e_contract_term_is_not_assignee_deadline_and_results_split() -> None:
    c = ctx([("Руководитель", "Салтанат, обновите договор и отправьте уведомление подрядчику. В договоре "
                              "пропишите: оплата в течение пяти рабочих дней после выполнения работ.")],
            [P("Салтанат")])
    cards = run(c, [
        {"type": "assign", "segment": "S1", "task": "N1", "action": "обновить договор", "action_quote": "обновите договор",
         "assignee": "Салтанат", "expected_result": "условие оплаты: пять рабочих дней после выполнения работ"},
        {"type": "assign", "segment": "S1", "task": "N2", "action": "отправить уведомление подрядчику",
         "action_quote": "отправьте уведомление подрядчику", "assignee": "Салтанат"},
    ])
    assert len(cards) == 2
    for card in cards:
        assert card["deadline"]["kind"] == "missing"
        assert "срок не назван" in card["review_reasons"]


def test_g_department_assignee_and_contact_person() -> None:
    c = ctx([("Руководитель", "Юридический департамент готовит заключение по неустойке. Данияр, свяжитесь с "
                              "юристами до среды.")], [P("Руководитель"), P("Данияр")])
    cards = run(c, [
        {"type": "assign", "segment": "S1", "task": "N1", "action": "подготовить заключение по неустойке",
         "action_quote": "готовит заключение по неустойке", "assignee": "Юридический департамент",
         "assignee_kind": "department", "assignee_quote": "Юридический департамент"},
        {"type": "assign", "segment": "S1", "task": "N2", "action": "связаться с юридическим департаментом",
         "action_quote": "свяжитесь с юристами", "assignee": "Данияр", "deadline_quote": "до среды"},
    ])
    legal = by_action(cards, "заключение")
    contact = by_action(cards, "связаться")
    assert legal["assignee_type"] == "department"
    assert contact["assignee"]["name"] == "Данияр" and contact["deadline"]["normalized_date"] == "2026-09-30"


def test_invented_quote_and_segment_are_caught() -> None:
    c = ctx([("Руководитель", "Ерлан, подготовьте отчёт к пятнице.")], [P("Ерлан")])
    linker = Linker(c)
    linker.begin_window()
    linker.apply(LlmEvent(type="assign", segment="S9", task="N1", action="x"), 0)
    linker.apply(LlmEvent(type="accept", segment="S1", task="T7"), 1)  # unknown task, not creating
    linker.apply(LlmEvent(type="assign", segment="S1", task="N1", action="подготовить отчёт",
                          action_quote="подготовить годовой бюджет", assignee="Ерлан", deadline_quote="к пятнице"), 2)
    assert [r["event_index"] for r in linker.rejected_events] == [0, 1]
    card = linker.cards()[0]
    assert any("основание" in r and "action" in r for r in card["review_reasons"])
    assert card["evidence"]["deadline"][0]["match"] == "exact"


def test_kazakh_and_mixed_assignments() -> None:
    c = ctx([("Басшы", "Ерлан, есепті жұмаға дейін дайындаңыз."),
             ("Басшы", "Айдана, подготовь смету, келесі аптаға дейін."),
             ("Басшы", "Бұл тапсырманы әзірге орындамаңыз.")], [P("Ерлан"), P("Айдана")])
    cards = run(c, [
        {"type": "assign", "segment": "S1", "task": "N1", "action": "подготовить отчёт", "action_quote": "есепті",
         "assignee": "Ерлан", "deadline_quote": "жұмаға дейін"},
        {"type": "assign", "segment": "S2", "task": "N2", "action": "подготовить смету",
         "action_quote": "подготовь смету", "assignee": "Айдана", "deadline_quote": "келесі аптаға дейін"},
        {"type": "suspend", "segment": "S3", "task": "T2", "action_quote": "әзірге орындамаңыз"},
    ])
    report = by_action(cards, "отчёт")
    assert report["deadline"]["normalized_date"] == "2026-09-25"
    estimate = by_action(cards, "смет")
    assert estimate["on_hold"] and estimate["deadline"]["ambiguous"]
    assert estimate["execution_state"] == "open"  # suspended is not cancelled


def test_duplicate_restatement_does_not_create_second_task() -> None:
    c = ctx([("Руководитель", "Ерлан, подготовьте претензию до пятницы."),
             ("Руководитель", "Ещё раз: Ерлан готовит претензию до пятницы.")], [P("Ерлан")])
    cards = run(c, [
        {"type": "assign", "segment": "S1", "task": "N1", "action": "подготовить претензию",
         "action_quote": "подготовьте претензию", "assignee": "Ерлан", "deadline_quote": "до пятницы"},
        {"type": "assign", "segment": "S2", "task": "N2", "action": "подготовить претензию",
         "action_quote": "готовит претензию", "assignee": "Ерлан", "deadline_quote": "до пятницы"},
    ])
    assert len(cards) == 1
    assert len(cards[0]["evidence"]["action"]) >= 2
