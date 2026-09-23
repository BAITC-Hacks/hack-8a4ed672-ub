"""Deterministic linking of extracted discussion events into action cards (docs/extraction-policy.md).

Principles:
* the last utterance is not automatically right: a `restate` (summary) never overwrites earlier agreed
  details — differences become review issues with both bases;
* a proposal ("Две недели достаточно?") is not an agreement until accepted; a rejected proposal is kept in
  history only;
* each field keeps its own evidence (segment + quote + transcript revision);
* conditional decisions stay conditional; suspensions put a task on hold; nothing is inferred from silence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from hattama.domain.deadlines import normalize_deadline
from hattama.domain.names import ParticipantRef, addressee_of, assignee_kind, match_participant, norm, stem
from hattama.extraction.evidence import EvidenceRef, SegmentView, verify
from hattama.extraction.schemas import LlmEvent

AGREED, PROPOSED, REJECTED = "agreed", "proposed", "rejected"
# meta-phrases a small model sometimes writes instead of the task content
CREATING_TYPES = ("assign", "propose", "conditional", "suspend", "restate")
GENERIC_ACTIONS = ("поставить задачу", "согласиться", "повторить", "решение с условием", "принять", "отказаться",
                   "отменить", "приостановить", "задача", "поручение", "выполнить задачу", "условное решение",
                   "предложить", "резюмировать")


@dataclass
class FieldValue:
    value: Any
    status: str
    evidence: list[EvidenceRef] = field(default_factory=list)
    source_event: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {"value": self.value, "status": self.status, "evidence": [e.to_dict() for e in self.evidence],
                "source_event": self.source_event}


@dataclass
class IssueDraft:
    kind: str
    severity: str
    field: str | None
    question: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Proposal:
    field: str
    value: FieldValue
    event_index: int


@dataclass
class TaskState:
    ref: str
    action: FieldValue
    created_by_event: str
    expected_result: FieldValue | None = None
    assignee: FieldValue | None = None
    assigner: FieldValue | None = None
    curator: FieldValue | None = None
    collaborators: list[str] = field(default_factory=list)
    deadline: FieldValue | None = None
    condition: FieldValue | None = None
    conditional: bool = False
    parent: str | None = None
    depends_on: list[str] = field(default_factory=list)
    topic: str | None = None
    status: str = AGREED  # task-level agreement
    execution: str = "open"
    on_hold: bool = False
    pending: list[Proposal] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    issues: list[IssueDraft] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    extra_evidence: list[EvidenceRef] = field(default_factory=list)
    first_seen_ms: int = 0


@dataclass
class LinkContext:
    meeting_date: date
    timezone: str
    participants: list[ParticipantRef]
    segments: dict[str, SegmentView]  # alias -> view
    ordered: list[SegmentView]


_VERBISH = re.compile(
    r"(ть|ти|чь|ться|ите|йте|ишь|ешь|ет|ит|ут|ют|ат|ят|ем|им|ал|ял|ла|ли|ло|ся|сь|ете|у|ю|ңыз|ңіз|сын|сін|ады|еді|"
    r"йды|йді|ды|ді|ты|ті|пыз|піз|мыз|міз|уы|уі)$")
DIRECTIVE_WORDS = ("надо", "нужно", "необходимо", "пусть", "должен", "должна", "должны", "поручаю", "прошу", "давайте",
                   "керек", "қажет", "тиіс", "тапсырамын")


def looks_like_directive(quote: str | None) -> bool:
    """Soft check: a task quote normally contains a verb form or a directive word (RU/KZ)."""
    if not quote:
        return False
    words = norm(quote).split()
    return any(w in DIRECTIVE_WORDS for w in words) or any(len(w) > 3 and _VERBISH.search(w) for w in words)


def first_stem(name: str | None) -> str:
    tokens = norm(name or "").split()
    return stem(tokens[0]) if tokens else ""


def _similar(a: str, b: str) -> float:
    ta = {stem(t) for t in norm(a).split() if len(t) > 2}
    tb = {stem(t) for t in norm(b).split() if len(t) > 2}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class Linker:
    def __init__(self, ctx: LinkContext, existing: list[TaskState] | None = None) -> None:
        self.ctx = ctx
        self.tasks: dict[str, TaskState] = {t.ref: t for t in (existing or [])}
        self.window_refs: dict[str, str] = {}  # N<n> -> T<m> for the current window
        self.global_issues: list[IssueDraft] = []
        self.rejected_events: list[dict[str, Any]] = []
        self.recovered_refs: list[str] = []
        self._counter = max([int(r[1:]) for r in self.tasks] or [0])

    # ------------------------------------------------------------------ helpers
    def _next_ref(self) -> str:
        self._counter += 1
        return f"T{self._counter}"

    def _resolve_ref(self, ref: str | None) -> str | None:
        if ref is None:
            return None
        if ref.startswith("N") or ref not in self.tasks:
            # unknown T-refs are window-local aliases too (a small model may number new tasks T1, T2 …)
            return self.window_refs.get(ref)
        return ref

    def begin_window(self) -> None:
        self.window_refs = {}

    def _ev(self, quote: str | None, alias: str) -> list[EvidenceRef]:
        if not quote:
            return []
        return [verify(quote, alias, self.ctx.segments, self.ctx.ordered)]

    def _assignee_value(self, e: LlmEvent) -> dict[str, Any] | None:
        if not e.assignee:
            return None
        kind = e.assignee_kind or assignee_kind(e.assignee)
        if kind == "person" and assignee_kind(e.assignee) == "department":
            kind = "department"
        value: dict[str, Any] = {"name": e.assignee.strip(), "kind": kind, "participant_id": None,
                                 "match_reason": None, "present": None}
        if kind == "person":
            match = match_participant(e.assignee, self.ctx.participants)
            if match is not None and match.score >= 0.6:
                value.update(participant_id=match.participant.id, name=match.participant.display_name,
                             match_reason=match.reason, present=match.participant.is_present,
                             match_score=match.score)
        return value

    def _person_value(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        match = match_participant(name, self.ctx.participants)
        if match is not None and match.score >= 0.6:
            return {"name": match.participant.display_name, "participant_id": match.participant.id,
                    "match_reason": match.reason}
        return {"name": name.strip(), "participant_id": None, "match_reason": None}

    def _speaker_assigner(self, alias: str) -> dict[str, Any] | None:
        seg = self.ctx.segments.get(alias)
        if seg is None or not seg.speaker_name:
            return None
        return {"name": seg.speaker_name, "participant_id": seg.speaker_participant_id,
                "match_reason": "говорящий сегмента (подтверждённая привязка)"}

    def _record(self, task: TaskState, kind: str, e: LlmEvent, idx: int, detail: dict[str, Any]) -> None:
        seg = self.ctx.segments.get(e.segment)
        task.history.append({"event": kind, "segment": seg.id if seg else None, "alias": e.segment,
                             "event_index": idx, **detail})

    # ------------------------------------------------------------------ event application
    def apply(self, e: LlmEvent, idx: int) -> None:
        if e.segment not in self.ctx.segments:
            self.rejected_events.append({"event_index": idx, "reason": f"несуществующий сегмент {e.segment}",
                                         "type": e.type})
            return
        target = self._resolve_ref(e.task)
        if e.task.startswith("T") and target is None and not (e.type in CREATING_TYPES and e.action):
            self.rejected_events.append({"event_index": idx, "reason": f"ссылка на несуществующую задачу {e.task}",
                                         "type": e.type})
            return
        if e.task.startswith("T") and target is None:
            self.recovered_refs.append(e.task)
        handler = getattr(self, f"_on_{e.type}")
        handler(e, idx, target)

    def _create(self, e: LlmEvent, idx: int, status: str) -> TaskState | None:
        if e.action and norm(e.action).strip() in GENERIC_ACTIONS:
            if e.action_quote and norm(e.action_quote) not in GENERIC_ACTIONS and len(e.action_quote) <= 120:
                e = e.model_copy(update={"action": e.action_quote})
            else:
                self.rejected_events.append({"event_index": idx, "type": e.type,
                                             "reason": f"действие не сформулировано («{e.action}»)"})
                return None
        if not e.action:
            self.rejected_events.append({"event_index": idx, "reason": "новая задача без действия", "type": e.type})
            return None
        duplicate = self._find_duplicate(e)
        if duplicate is not None:
            if e.task.startswith("N") or e.task not in self.tasks:
                self.window_refs[e.task] = duplicate.ref
            self._on_restate(e, idx, duplicate.ref)
            return None
        ref = self._next_ref()
        if e.task.startswith("N") or e.task not in self.tasks:
            self.window_refs[e.task] = ref
        action = FieldValue(e.action.strip(), status, self._ev(e.action_quote, e.segment), e.type)
        seg = self.ctx.segments[e.segment]
        task = TaskState(ref=ref, action=action, created_by_event=e.type, status=status, first_seen_ms=seg.start_ms)
        self._fill(task, e, status, idx)
        task.topic = e.topic
        parent = self._resolve_ref(e.parent)
        task.parent = parent if parent != ref else None
        task.depends_on = [r for r in (self._resolve_ref(d) for d in e.depends_on) if r]
        self._record(task, "created", e, idx, {"status": status})
        self.tasks[ref] = task
        return task

    def _fill(self, task: TaskState, e: LlmEvent, status: str, idx: int) -> None:
        assignee = self._assignee_value(e)
        if assignee:
            task.assignee = FieldValue(assignee, status, self._ev(e.assignee_quote or e.assignee, e.segment), e.type)
        assigner = self._speaker_assigner(e.segment) or self._person_value(e.assigner)
        if assigner:
            task.assigner = FieldValue(assigner, AGREED, [], e.type)
        if e.curator:
            task.curator = FieldValue(self._person_value(e.curator), status, [], e.type)
        if e.collaborators:
            task.collaborators = [c.strip() for c in e.collaborators if c.strip()]
        if e.deadline_quote:
            task.deadline = FieldValue(e.deadline_quote.strip(), status, self._ev(e.deadline_quote, e.segment), e.type)
        if e.condition_quote:
            task.condition = FieldValue(e.condition_quote.strip(), AGREED, self._ev(e.condition_quote, e.segment),
                                        e.type)
        if e.expected_result:
            task.expected_result = FieldValue(e.expected_result.strip(), status, [], e.type)

    def _find_duplicate(self, e: LlmEvent) -> TaskState | None:
        if not e.action:
            return None
        for task in self.tasks.values():
            if task.status == REJECTED or task.execution == "cancelled":
                continue
            same_assignee = (task.assignee is None or not e.assignee
                             or first_stem(task.assignee.value.get("name")) == first_stem(e.assignee))
            if same_assignee and _similar(task.action.value, e.action) >= 0.6:
                return task
        return None

    def _on_assign(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None:
            self._create(e, idx, AGREED)
            return
        task = self.tasks[target]
        updates = {}
        if e.deadline_quote and (task.deadline is None or task.deadline.status != AGREED):
            task.deadline = FieldValue(e.deadline_quote.strip(), AGREED, self._ev(e.deadline_quote, e.segment), e.type)
            updates["deadline"] = e.deadline_quote
        if e.assignee and task.assignee is None:
            assignee = self._assignee_value(e)
            task.assignee = FieldValue(assignee, AGREED, self._ev(e.assignee_quote or e.assignee, e.segment), e.type)
            updates["assignee"] = e.assignee
        if e.condition_quote and task.condition is None:
            task.condition = FieldValue(e.condition_quote, AGREED, self._ev(e.condition_quote, e.segment), e.type)
        if e.expected_result and task.expected_result is None:
            task.expected_result = FieldValue(e.expected_result, AGREED, [], e.type)
        if task.status == PROPOSED:
            task.status = AGREED
            task.action.status = AGREED
            updates["status"] = AGREED
        if e.action_quote:
            task.extra_evidence += self._ev(e.action_quote, e.segment)
        conflicts = self._conflicts(task, e)
        if conflicts:
            self._conflict_issue(task, e, idx, conflicts, "уточнение")
        self._record(task, "assign_update", e, idx, {"updates": updates})

    def _on_propose(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None:
            task = self._create(e, idx, PROPOSED)
            if task is not None:
                task.reasons.append("поручение прозвучало как предложение; согласие не зафиксировано")
            return
        task = self.tasks[target]
        if e.deadline_quote:
            task.pending.append(Proposal("deadline", FieldValue(e.deadline_quote.strip(), PROPOSED,
                                                                self._ev(e.deadline_quote, e.segment), e.type), idx))
        if e.assignee:
            task.pending.append(Proposal("assignee", FieldValue(self._assignee_value(e), PROPOSED,
                                                                self._ev(e.assignee_quote or e.assignee, e.segment),
                                                                e.type), idx))
        self._record(task, "proposal", e, idx, {"deadline": e.deadline_quote, "assignee": e.assignee})

    def _same_deadline(self, a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        if norm(a) == norm(b):
            return True
        da = normalize_deadline(a, self.ctx.meeting_date, self.ctx.timezone)
        db = normalize_deadline(b, self.ctx.meeting_date, self.ctx.timezone)
        return da.kind not in ("ambiguous", "missing") and da.normalized_date is not None and \
            da.normalized_date == db.normalized_date

    def _latest_pending(self, task: TaskState, fname: str) -> Proposal | None:
        items = [p for p in task.pending if p.field == fname]
        return max(items, key=lambda p: p.event_index) if items else None

    def _on_accept(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None:
            return
        task = self.tasks[target]
        accepted: list[str] = []
        ev = self._ev(e.deadline_quote or e.action_quote, e.segment)
        if e.deadline_quote:
            pend = self._latest_pending(task, "deadline")
            if pend is not None and self._same_deadline(pend.value.value, e.deadline_quote):
                pend.value.status = AGREED
                pend.value.evidence += ev
                self._set_field(task, "deadline", pend.value)
            elif task.deadline is not None and self._same_deadline(task.deadline.value, e.deadline_quote):
                task.deadline.status = AGREED
                task.deadline.evidence += ev
            else:
                # acceptance that names the value ("Хорошо, три недели") agrees to THAT value
                self._set_field(task, "deadline", FieldValue(e.deadline_quote, AGREED, ev, e.type))
            for p in [p for p in task.pending if p.field == "deadline"]:
                if p.value.status != AGREED:
                    task.history.append({"event": "proposal_superseded", "field": "deadline",
                                         "value": p.value.snapshot()})
            task.pending = [p for p in task.pending if p.field != "deadline"]
            accepted.append("deadline")
        if task.pending:
            latest_idx = max(p.event_index for p in task.pending)
            for p in [p for p in task.pending if p.event_index == latest_idx]:
                p.value.status = AGREED
                p.value.evidence += ev
                self._set_field(task, p.field, p.value)
                accepted.append(p.field)
            task.pending = [p for p in task.pending if p.event_index != latest_idx]
        if task.deadline is not None and task.deadline.status == PROPOSED:
            task.deadline.status = AGREED
            task.deadline.evidence += ev
            accepted.append("deadline")
        if task.status == PROPOSED:
            task.status = AGREED
            task.action.status = AGREED
            for fv in (task.assignee, task.expected_result):
                if fv is not None and fv.status == PROPOSED:
                    fv.status = AGREED
            accepted.append("task")
            task.reasons = [r for r in task.reasons if "предложение" not in r]
        self._record(task, "accepted", e, idx, {"fields": sorted(set(accepted))})

    def _on_reject(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None:
            return
        task = self.tasks[target]
        seg_id = self.ctx.segments[e.segment].id
        rejected_value: str | None = None
        if task.pending:
            latest_idx = max(p.event_index for p in task.pending)
            for p in [p for p in task.pending if p.event_index == latest_idx]:
                p.value.status = REJECTED
                if p.field == "deadline":
                    rejected_value = p.value.value
                task.history.append({"event": "proposal_rejected", "field": p.field, "value": p.value.snapshot(),
                                     "segment": seg_id})
            task.pending = [p for p in task.pending if p.event_index != latest_idx]
        elif task.deadline is not None and task.deadline.status == PROPOSED and (e.deadline_quote or
                                                                               task.status != PROPOSED):
            # objection to the proposed deadline, not to the task
            rejected_value = task.deadline.value
            task.deadline.status = REJECTED
            task.history.append({"event": "proposal_rejected", "field": "deadline", "value": task.deadline.snapshot(),
                                 "segment": seg_id})
            task.deadline = None
        elif task.status == PROPOSED:
            task.status = REJECTED
        counter = None
        if e.deadline_quote and not self._same_deadline(e.deadline_quote, rejected_value):
            # counter-proposal inside the objection ("нет, нужно три недели"); quoting the rejected value is not one
            counter = e.deadline_quote
            task.pending.append(Proposal("deadline", FieldValue(e.deadline_quote, PROPOSED,
                                                                self._ev(e.deadline_quote, e.segment), e.type), idx))
        self._record(task, "rejected", e, idx, {"rejected": rejected_value, "counter_deadline": counter})

    def _set_field(self, task: TaskState, name: str, value: FieldValue) -> None:
        old = getattr(task, name)
        if old is not None and old.value != value.value:
            task.history.append({"event": "changed", "field": name, "old": old.snapshot(), "new": value.snapshot()})
        setattr(task, name, value)

    def _on_change_deadline(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None or not e.deadline_quote:
            return
        task = self.tasks[target]
        self._set_field(task, "deadline", FieldValue(e.deadline_quote, AGREED, self._ev(e.deadline_quote, e.segment),
                                                     e.type))
        task.pending = [p for p in task.pending if p.field != "deadline"]
        self._record(task, "deadline_changed", e, idx, {"deadline": e.deadline_quote})

    def _on_change_assignee(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None or not e.assignee:
            return
        task = self.tasks[target]
        self._set_field(task, "assignee", FieldValue(self._assignee_value(e), AGREED,
                                                     self._ev(e.assignee_quote or e.assignee, e.segment), e.type))
        self._record(task, "assignee_changed", e, idx, {"assignee": e.assignee})

    def _on_cancel(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None:
            self.global_issues.append(IssueDraft("unlinked_cancellation", "warning", None,
                                                 "Прозвучала отмена, но не ясно, какого поручения она касается",
                                                 {"segment": self.ctx.segments[e.segment].id}))
            return
        task = self.tasks[target]
        task.execution = "cancelled"
        task.extra_evidence += self._ev(e.action_quote, e.segment)
        self._record(task, "cancelled", e, idx, {})

    def _on_suspend(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None:
            task = self._create(e, idx, AGREED)
            if task is None:
                return
        else:
            task = self.tasks[target]
        task.on_hold = True
        task.reasons.append("поручение приостановлено («пока не выполнять») — уточните, когда возобновить")
        task.extra_evidence += self._ev(e.action_quote, e.segment)
        self._record(task, "suspended", e, idx, {})

    def _on_conditional(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is not None and e.action and _similar(e.action, self.tasks[target].action.value) < 0.3:
            target = None  # the conditional decision is about something else -> its own task
            e = e.model_copy(update={"task": f"N{900 + idx}"})
        task = self._create(e, idx, AGREED) if target is None else self.tasks[target]
        if task is None:
            return
        task.conditional = True
        if e.condition_quote:
            task.condition = FieldValue(e.condition_quote, AGREED, self._ev(e.condition_quote, e.segment), e.type)
        else:
            task.reasons.append("условное решение без явно процитированного условия")
        self._record(task, "conditional", e, idx, {"condition": e.condition_quote})

    def _conflicts(self, task: TaskState, e: LlmEvent) -> dict[str, tuple[Any, Any]]:
        conflicts: dict[str, tuple[Any, Any]] = {}
        if e.assignee and task.assignee is not None and task.assignee.status == AGREED:
            new = self._assignee_value(e) or {}
            old_name = task.assignee.value.get("name", "")
            if first_stem(old_name) != first_stem(new.get("name")):
                conflicts["assignee"] = (old_name, new.get("name"))
        if e.deadline_quote and task.deadline is not None and task.deadline.status == AGREED:
            old = normalize_deadline(task.deadline.value, self.ctx.meeting_date, self.ctx.timezone)
            new = normalize_deadline(e.deadline_quote, self.ctx.meeting_date, self.ctx.timezone)
            if old.normalized_date != new.normalized_date or old.kind != new.kind:
                conflicts["deadline"] = (task.deadline.value, e.deadline_quote)
        return conflicts

    def _conflict_issue(self, task: TaskState, e: LlmEvent, idx: int, conflicts: dict[str, tuple[Any, Any]],
                        what: str) -> None:
        seg = self.ctx.segments[e.segment]
        for fname, (old, new) in conflicts.items():
            label = {"assignee": "исполнитель", "deadline": "срок"}[fname]
            old_ev = getattr(task, fname).evidence if getattr(task, fname) else []
            task.issues.append(IssueDraft(
                "restatement_conflict", "blocker", fname,
                f"{what.capitalize()} расходится с ранее согласованным: {label} «{old}» → «{new}». "
                "Какой вариант верный?",
                {"previous": {"value": old, "evidence": [x.to_dict() for x in old_ev]},
                 "later": {"value": new, "segment_id": seg.id, "alias": e.segment},
                 "policy": "позднее резюме не перезаписывает согласованные детали без подтверждения"}))
            task.reasons.append(f"{label}: противоречие между обсуждением и итоговым резюме")

    def _on_restate(self, e: LlmEvent, idx: int, target: str | None) -> None:
        if target is None:
            # summary of something never discussed as a task: create a proposed task, flagged
            task = self._create(e, idx, PROPOSED)
            if task is not None:
                task.reasons.append("задача появилась только в итоговом резюме — проверьте, было ли решение")
            return
        task = self.tasks[target]
        conflicts = self._conflicts(task, e)
        if conflicts:
            self._conflict_issue(task, e, idx, conflicts, "итоговое резюме")
        else:
            if e.deadline_quote and task.deadline is not None:
                task.deadline.evidence += self._ev(e.deadline_quote, e.segment)
            if e.assignee and task.assignee is not None:
                task.assignee.evidence += self._ev(e.assignee_quote or e.assignee, e.segment)
            if e.deadline_quote and task.deadline is None:
                task.deadline = FieldValue(e.deadline_quote, AGREED, self._ev(e.deadline_quote, e.segment), e.type)
                task.reasons.append("срок назван только в итоговом резюме")
            if e.assignee and task.assignee is None:
                task.assignee = FieldValue(self._assignee_value(e), AGREED,
                                           self._ev(e.assignee_quote or e.assignee, e.segment), e.type)
        task.extra_evidence += self._ev(e.action_quote, e.segment)
        self._record(task, "restated", e, idx, {"conflicts": list(conflicts)})

    # ------------------------------------------------------------------ finalisation
    def cards(self) -> list[dict[str, Any]]:
        """Materialise action cards with typed deadlines, review reasons and issues."""
        out = []
        for task in sorted(self.tasks.values(), key=lambda t: (t.first_seen_ms, int(t.ref[1:]))):
            out.append(self._card(task))
        return out

    def _card(self, task: TaskState) -> dict[str, Any]:
        reasons = list(dict.fromkeys(task.reasons))
        issues = list(task.issues)
        # unresolved proposals
        deadline_fv = task.deadline
        for p in task.pending:
            if p.field == "deadline" and deadline_fv is None:
                deadline_fv = p.value
                reasons.append("срок только предложен, согласие не зафиксировано")
            else:
                reasons.append(f"есть неподтверждённое предложение по полю «{p.field}»")
        deadline = normalize_deadline(deadline_fv.value if deadline_fv else None, self.ctx.meeting_date,
                                      self.ctx.timezone)
        if deadline.needs_review:
            reasons.append("срок неоднозначен или противоречив")
            if deadline.conflict:
                issues.append(IssueDraft("deadline_conflict", "blocker", "deadline",
                                         f"Срок «{deadline.raw_text}» содержит противоречие. Какая дата верна?",
                                         {"deadline": deadline.to_dict()}))
        if deadline.kind == "missing":
            reasons.append("срок не назван")
        if deadline.kind == "event":
            reasons.append("срок зависит от события — календарная дата неизвестна")
        assignee = task.assignee.value if task.assignee else None
        assignee_type = "unknown"
        creation_seg = next((self.ctx.segments.get(h["alias"]) for h in task.history if h["event"] == "created"), None)
        if assignee is None and creation_seg is not None:
            addressed = addressee_of(creation_seg.text)
            match = match_participant(addressed, self.ctx.participants) if addressed else None
            if match is not None and match.score >= 0.75:
                assignee = {"name": match.participant.display_name, "kind": "person",
                            "participant_id": match.participant.id, "present": match.participant.is_present,
                            "match_reason": "предположено по обращению в начале реплики", "match_score": 0.7}
                reasons.append(f"исполнитель «{match.participant.display_name}» предположен по обращению — "
                               "подтвердите")
        if assignee is None:
            reasons.append("исполнитель не назван")
            issues.append(IssueDraft("missing_assignee", "warning", "assignee",
                                     f"Кто исполнитель поручения «{task.action.value}»?"))
        else:
            kind = assignee.get("kind", "person")
            if kind == "department":
                assignee_type = "department"
            elif kind == "group":
                assignee_type = "group"
                reasons.append("исполнитель — группа; уточните ответственного")
            elif assignee.get("participant_id"):
                assignee_type = "participant" if assignee.get("present") else "mentioned_person"
                if (assignee.get("match_score") or 1.0) < 0.9:
                    reasons.append(f"имя исполнителя сопоставлено неточно ({assignee.get('match_reason')})")
            else:
                assignee_type = "mentioned_person"
                reasons.append(f"«{assignee.get('name')}» нет в списке участников — сотрудник упомянут, но не "
                               "обязательно присутствовал")
        # addressee vs assignee (scenario B: «Ботагоз, пусть Ерлан …»)
        if creation_seg is not None and assignee is not None:
            addressed = addressee_of(creation_seg.text)
            if addressed and first_stem(addressed) != first_stem(assignee.get("name")):
                if task.curator is None:
                    reasons.append(f"реплика обращена к «{addressed}», исполнитель — «{assignee.get('name')}»: "
                                   "проверьте роли исполнителя и куратора")
        # evidence checks per field
        evidence = {"action": [*task.action.evidence, *task.extra_evidence],
                    "assignee": task.assignee.evidence if task.assignee else [],
                    "deadline": deadline_fv.evidence if deadline_fv else [],
                    "condition": task.condition.evidence if task.condition else []}
        for fname, refs in evidence.items():
            if fname in ("condition",) and task.condition is None:
                continue
            if fname == "deadline" and deadline_fv is None:
                continue
            if fname == "assignee" and task.assignee is None:
                continue
            if not refs or all(r.match == "missing" for r in refs):
                reasons.append(f"не найдено основание в тексте для поля «{fname}»")
            elif any(r.note and "соседнем" in r.note for r in refs):
                reasons.append(f"основание поля «{fname}» найдено в соседнем сегменте")
        if task.conditional and task.condition is None:
            reasons.append("условное решение без зафиксированного условия")
        action_quotes = [r.quote for r in task.action.evidence if r.match != "missing"]
        if action_quotes and not any(looks_like_directive(q) for q in action_quotes):
            reasons.append("в цитате нет явного поручения — проверьте, было ли решение")
        review_state = "rejected" if task.status == REJECTED else ("needs_review" if reasons or issues else "proposed")
        card = {
            "ref": task.ref,
            "action": task.action.value,
            "expected_result": task.expected_result.value if task.expected_result else None,
            "assigner": task.assigner.value if task.assigner else None,
            "assignee": assignee,
            "assignee_type": assignee_type,
            "curator": task.curator.value if task.curator else None,
            "collaborators": task.collaborators,
            "deadline": deadline.to_dict(),
            "condition": task.condition.value if task.condition else None,
            "conditional": task.conditional,
            "dependencies": [{"ref": d} for d in task.depends_on] + (
                [{"event": deadline.anchor_event}] if deadline.kind == "event" and deadline.anchor_event else []),
            "parent_ref": task.parent,
            "topic": task.topic,
            "review_state": review_state,
            "execution_state": task.execution,
            "on_hold": task.on_hold,
            "agreement": task.status,
            "review_reasons": list(dict.fromkeys(reasons)),
            "issues": [i.__dict__ for i in issues],
            "evidence": {k: [r.to_dict() for r in v] for k, v in evidence.items()},
            "history": task.history,
            "first_seen_ms": task.first_seen_ms,
        }
        card["fingerprint"] = fingerprint(card)
        return card


def fingerprint(card: dict[str, Any]) -> str:
    action = " ".join(sorted({stem(t) for t in norm(card["action"]).split() if len(t) > 2}))
    assignee = card["assignee"]["name"] if card.get("assignee") else ""
    return hashlib.sha256(f"{action}|{norm(assignee)}".encode()).hexdigest()[:32]


def state_summary(tasks: list[TaskState], limit_chars: int = 1400) -> str:
    """Compact registry state passed to the next extraction window (keeps discussion state)."""
    lines = []
    for t in tasks:
        if t.status == REJECTED:
            continue
        who = t.assignee.value.get("name") if t.assignee else "?"
        dl = t.deadline.value if t.deadline else "—"
        extra = []
        if t.pending:
            extra.append("есть предложение: " + ", ".join(f"{p.field}={p.value.value}" for p in t.pending
                                                           if not isinstance(p.value.value, dict)))
        if t.status == PROPOSED:
            extra.append("не согласовано")
        if t.execution == "cancelled":
            extra.append("отменено")
        if t.on_hold:
            extra.append("приостановлено")
        if t.conditional:
            extra.append("условное")
        line = f"{t.ref}: [{who}] {t.action.value}; срок: {dl}" + (f" ({'; '.join(extra)})" if extra else "")
        lines.append(re.sub(r"\s+", " ", line))
    text = "\n".join(lines)
    return text[-limit_chars:] if len(text) > limit_chars else text
