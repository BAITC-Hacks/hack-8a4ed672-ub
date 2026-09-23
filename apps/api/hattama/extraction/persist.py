"""Persist extraction results. Human-edited/confirmed data is never overwritten by machine output:
differences are stored as a new ActionRevision proposal plus a review issue."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hattama.db.models import ActionItem, ActionRevision, EvidenceSpan, ReviewIssue
from hattama.domain.enums import ReviewState
from hattama.services.events import publish

PROTECTED = (ReviewState.EDITED, ReviewState.CONFIRMED)
FIELDS = ("action", "expected_result", "assigner", "assignee", "assignee_type", "curator", "collaborators", "deadline",
          "condition", "conditional", "dependencies", "topic", "on_hold")


def snapshot(item: ActionItem) -> dict[str, Any]:
    return {f: getattr(item, f) for f in FIELDS} | {"review_state": item.review_state,
                                                  "execution_state": item.execution_state}


def add_issue(session: Session, meeting_id: str, key: str, kind: str, severity: str, question: str,
              object_type: str | None = None, object_id: str | None = None, field: str | None = None,
              details: dict[str, Any] | None = None) -> None:
    exists = session.scalar(select(ReviewIssue).where(ReviewIssue.meeting_id == meeting_id,
                                                      ReviewIssue.dedupe_key == key))
    if exists is None:
        session.add(ReviewIssue(meeting_id=meeting_id, dedupe_key=key[:200], kind=kind, severity=severity,
                                question=question, object_type=object_type, object_id=object_id, field=field,
                                details=details or {}))


def persist_cards(session: Session, meeting_id: str, run_id: str, origin: str, cards: list[dict[str, Any]]) -> dict:
    stats = {"created": 0, "updated": 0, "protected": 0}
    number = session.scalar(select(func.max(ActionItem.number)).where(ActionItem.meeting_id == meeting_id)) or 0
    ref_to_id: dict[str, str] = {}
    for card in cards:
        item = session.scalar(select(ActionItem).where(ActionItem.meeting_id == meeting_id,
                                                       ActionItem.fingerprint == card["fingerprint"]))
        values = {
            "action": card["action"], "expected_result": card["expected_result"], "assigner": card["assigner"],
            "assignee": card["assignee"], "assignee_type": card["assignee_type"], "curator": card["curator"],
            "collaborators": card["collaborators"], "deadline": card["deadline"], "condition": card["condition"],
            "conditional": card["conditional"], "dependencies": card["dependencies"], "topic": card["topic"],
            "on_hold": card["on_hold"],
        }
        if item is not None and item.review_state in PROTECTED:
            stats["protected"] += 1
            n = (session.scalar(select(func.max(ActionRevision.number)).where(ActionRevision.action_id == item.id))
                 or 0) + 1
            session.add(ActionRevision(action_id=item.id, number=n, change_kind="machine_proposal",
                                       reason=f"{origin}: новая машинная версия; ручные данные сохранены",
                                       snapshot=values, event={"run_id": run_id}))
            add_issue(session, meeting_id, f"compare:{item.id}:{run_id}", "machine_version_differs", "info",
                      "Автоматический проход предложил другую версию поручения, отредактированного вручную. "
                      "Сравните версии.", "action", item.id)
            ref_to_id[card["ref"]] = item.id
            continue
        if item is None:
            number += 1
            item = ActionItem(meeting_id=meeting_id, number=number, fingerprint=card["fingerprint"], origin=origin,
                              **values)
            session.add(item)
            stats["created"] += 1
        else:
            for k, v in values.items():
                setattr(item, k, v)
            stats["updated"] += 1
        item.review_state = card["review_state"]
        item.execution_state = card["execution_state"]
        item.review_reasons = card["review_reasons"]
        item.extraction_run_id = run_id
        item.local_ref = card["ref"]
        session.flush()
        ref_to_id[card["ref"]] = item.id
        n = (session.scalar(select(func.max(ActionRevision.number)).where(ActionRevision.action_id == item.id)) or 0) + 1
        session.add(ActionRevision(action_id=item.id, number=n, change_kind=f"{origin}_extraction", snapshot=values,
                                   event={"run_id": run_id, "history": card["history"][-20:]}))
        for ev in session.scalars(select(EvidenceSpan).where(EvidenceSpan.action_id == item.id,
                                                             EvidenceSpan.superseded.is_(False))):
            ev.superseded = True
        for field_name, refs in card["evidence"].items():
            for ref in refs:
                if not ref.get("segment_id"):
                    continue
                session.add(EvidenceSpan(meeting_id=meeting_id, action_id=item.id, field=field_name,
                                         segment_id=ref["segment_id"], transcript_revision_id=ref["revision_id"],
                                         quote=ref["quote"], char_start=ref.get("char_start"),
                                         char_end=ref.get("char_end"), start_ms=ref.get("start_ms"),
                                         end_ms=ref.get("end_ms"), match=ref["match"]))
        for issue in card["issues"]:
            add_issue(session, meeting_id, f"{issue['kind']}:{item.fingerprint}:{issue.get('field')}", issue["kind"],
                      issue["severity"], issue["question"], "action", item.id, issue.get("field"), issue["details"])
        publish(session, meeting_id, "action.preliminary",
                {"id": item.id, "action": item.action, "assignee": item.assignee, "deadline": item.deadline,
                 "review_state": item.review_state, "origin": origin})
    for card in cards:  # parent links after all ids exist
        if card.get("parent_ref") and card["ref"] in ref_to_id and card["parent_ref"] in ref_to_id:
            item = session.get(ActionItem, ref_to_id[card["ref"]])
            if item is not None and item.review_state not in PROTECTED:
                item.parent_id = ref_to_id[card["parent_ref"]]
    return stats
