"""Review, approval, action registry, exports, audio playback and notifications.
Every endpoint checks object-level access; file downloads are authorised, not secret-URL based."""

from __future__ import annotations

import struct
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from hattama.api.deps import can_update_execution, can_view_action, current_user, get_db, meeting_for, member_role
from hattama.api.routes.live import segment_dict
from hattama.config import Settings, get_settings
from hattama.db.models import (
    ActionItem,
    ActionRevision,
    AuditEvent,
    CaptureSession,
    CaptureSource,
    EvidenceSpan,
    ExportArtifact,
    Meeting,
    MeetingMember,
    Notification,
    Participant,
    ProtocolApproval,
    ReviewIssue,
    Speaker,
    SpeakerBinding,
    SummaryRevision,
    TranscriptRevision,
    TranscriptSegment,
    User,
)
from hattama.db.types import utcnow
from hattama.domain import meeting_status
from hattama.domain.enums import MeetingStatus, ReviewState
from hattama.export.protocol import build_snapshot, render_docx, render_pdf, snapshot_hash
from hattama.jobs.queue import enqueue
from hattama.services.events import audit

router = APIRouter(prefix="/api/v1", tags=["review"])


# ---------------------------------------------------------------- transcript
@router.get("/meetings/{meeting_id}/transcript")
def transcript(meeting_id: str, revision_id: str | None = None, q: str | None = None,
               user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    meeting = meeting_for(db, user, meeting_id, "read")
    rev_id = revision_id or meeting.active_transcript_revision_id
    if rev_id is None:
        live = db.scalar(select(TranscriptRevision.id).where(TranscriptRevision.meeting_id == meeting_id,
                                                             TranscriptRevision.kind == "live"))
        rev_id = live
    revisions = [{"id": r.id, "number": r.number, "kind": r.kind, "status": r.status, "asr_model": r.asr_model,
                  "created_at": r.created_at} for r in db.scalars(
        select(TranscriptRevision).where(TranscriptRevision.meeting_id == meeting_id).order_by(TranscriptRevision.number))]
    if rev_id is None:
        return {"revision_id": None, "revisions": revisions, "segments": [], "speakers": []}
    stmt = select(TranscriptSegment).where(TranscriptSegment.revision_id == rev_id, TranscriptSegment.meeting_id ==
                                           meeting_id).order_by(TranscriptSegment.start_ms)
    if q:
        stmt = stmt.where(TranscriptSegment.text.ilike(f"%{q[:100]}%"))
    speakers = []
    for sp in db.scalars(select(Speaker).where(Speaker.meeting_id == meeting_id, Speaker.revision_id == rev_id)):
        b = db.scalar(select(SpeakerBinding).where(SpeakerBinding.speaker_id == sp.id)
                      .order_by(SpeakerBinding.created_at.desc()).limit(1))
        p = db.get(Participant, b.participant_id) if b else None
        speakers.append({"id": sp.id, "label": sp.label, "source": sp.source_key, "merged_into": sp.merged_into_id,
                         "binding": {"participant_id": b.participant_id, "name": p.display_name if p else None,
                                     "status": b.status, "reason": b.reason} if b else None})
    return {"revision_id": rev_id, "revisions": revisions, "speakers": speakers,
            "segments": [segment_dict(s) for s in db.scalars(stmt)]}


class SegmentPatch(BaseModel):
    rev: int
    text: str | None = Field(default=None, max_length=10000)
    speaker_id: str | None = None


@router.patch("/segments/{segment_id}")
def edit_segment(segment_id: str, body: SegmentPatch, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    seg = db.get(TranscriptSegment, segment_id)
    if seg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сегмент не найден")
    meeting = meeting_for(db, user, seg.meeting_id, "edit")
    if meeting.status == MeetingStatus.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Протокол утверждён; создайте новую версию")
    if seg.rev != body.rev:
        raise HTTPException(status.HTTP_409_CONFLICT, "Сегмент изменён другим пользователем или системой")
    before = {"text": seg.text, "speaker_id": seg.speaker_id}
    if body.text is not None:
        seg.text = body.text.strip()
    if body.speaker_id is not None:
        if db.get(Speaker, body.speaker_id) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Неизвестный говорящий")
        seg.speaker_id = body.speaker_id
    seg.edited_by = user.id
    seg.edited_at = utcnow()
    db.flush()
    audit(db, action="segment.edited", actor_user_id=user.id, meeting_id=seg.meeting_id, object_type="segment",
          object_id=seg.id, before=before, after={"text": seg.text, "speaker_id": seg.speaker_id})
    return segment_dict(seg)


class BindingIn(BaseModel):
    participant_id: str | None
    action: Literal["confirm", "reject"] = "confirm"
    merge_into_speaker_id: str | None = None


@router.post("/speakers/{speaker_id}/binding")
def bind_speaker(speaker_id: str, body: BindingIn, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    sp = db.get(Speaker, speaker_id)
    if sp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Говорящий не найден")
    meeting_for(db, user, sp.meeting_id, "edit")
    if body.merge_into_speaker_id:
        target = db.get(Speaker, body.merge_into_speaker_id)
        if target is None or target.meeting_id != sp.meeting_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Неверная цель объединения")
        sp.merged_into_id = target.id
        for seg in db.scalars(select(TranscriptSegment).where(TranscriptSegment.speaker_id == sp.id)):
            seg.speaker_id = target.id
        audit(db, action="speaker.merged", actor_user_id=user.id, meeting_id=sp.meeting_id, object_type="speaker",
              object_id=sp.id, after={"into": target.id})
        return {"merged_into": target.id}
    if body.participant_id:
        p = db.get(Participant, body.participant_id)
        if p is None or p.meeting_id != sp.meeting_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Участник не из этой встречи")
        db.add(SpeakerBinding(speaker_id=sp.id, participant_id=p.id,
                              status="confirmed" if body.action == "confirm" else "rejected",
                              reason="подтверждено секретарём" if body.action == "confirm" else "отклонено секретарём",
                              created_by=user.id))
    audit(db, action=f"speaker.binding_{body.action}", actor_user_id=user.id, meeting_id=sp.meeting_id,
          object_type="speaker", object_id=sp.id, after={"participant_id": body.participant_id})
    return {"ok": True}


# ---------------------------------------------------------------- actions
def action_out(db: Session, a: ActionItem, today: date | None = None, *, user: User | None = None) -> dict[str, Any]:
    ev = [{"field": e.field, "segment_id": e.segment_id, "quote": e.quote, "start_ms": e.start_ms,
           "end_ms": e.end_ms, "match": e.match}
          for e in db.scalars(select(EvidenceSpan).where(EvidenceSpan.action_id == a.id,
                                                         EvidenceSpan.superseded.is_(False)))]
    return {"id": a.id, "meeting_id": a.meeting_id, "number": a.number, "action": a.action,
            "expected_result": a.expected_result, "assigner": a.assigner, "assignee": a.assignee,
            "assignee_type": a.assignee_type, "curator": a.curator, "collaborators": a.collaborators,
            "deadline": a.deadline, "deadline_confirmed": a.deadline_confirmed, "condition": a.condition,
            "conditional": a.conditional, "dependencies": a.dependencies, "parent_id": a.parent_id, "topic": a.topic,
            "review_state": a.review_state, "execution_state": a.execution_state, "on_hold": a.on_hold,
            "review_reasons": a.review_reasons, "origin": a.origin, "version": a.version, "evidence": ev,
            "overdue": is_overdue(a, today),
            "can_update_execution": can_update_execution(db, user, a) if user else False,
            "can_view_meeting": member_role(db, a.meeting_id, user.id) is not None if user else False}


def is_overdue(a: ActionItem, today: date | None = None) -> bool:
    """Overdue only for a CONFIRMED calendar deadline of an unfinished, non-conditional task."""
    d = a.deadline or {}
    if not a.deadline_confirmed or not d.get("normalized_date") or a.conditional or a.on_hold:
        return False
    if a.execution_state not in ("open", "in_progress"):
        return False
    tz = ZoneInfo(d.get("timezone") or "Asia/Almaty")
    today = today or datetime.now(tz).date()
    return date.fromisoformat(d["normalized_date"]) < today


@router.get("/meetings/{meeting_id}/actions")
def meeting_actions(meeting_id: str, include_rejected: bool = False, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    meeting_for(db, user, meeting_id, "read")
    stmt = select(ActionItem).where(ActionItem.meeting_id == meeting_id).order_by(ActionItem.number)
    if not include_rejected:
        stmt = stmt.where(ActionItem.review_state != ReviewState.REJECTED)
    return [action_out(db, a, user=user) for a in db.scalars(stmt)]


class ActionPatch(BaseModel):
    version: int
    action: str | None = Field(default=None, max_length=1000)
    assignee_name: str | None = Field(default=None, max_length=200)
    assignee_participant_id: str | None = None
    assignee_type: Literal["participant", "mentioned_person", "department", "group", "unknown"] | None = None
    curator_name: str | None = Field(default=None, max_length=200)
    deadline_date: date | None = None
    deadline_raw: str | None = Field(default=None, max_length=200)
    deadline_missing: bool = False
    condition: str | None = Field(default=None, max_length=1000)
    review_state: Literal["confirmed", "rejected", "needs_review"] | None = None


def _editable(db: Session, user: User, a: ActionItem) -> Meeting:
    meeting = meeting_for(db, user, a.meeting_id, "edit")
    if meeting.status == MeetingStatus.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Протокол утверждён; создайте новую версию для правок")
    return meeting


@router.patch("/actions/{action_id}")
def patch_action(action_id: str, body: ActionPatch, user: User = Depends(current_user),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    a = db.get(ActionItem, action_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Поручение не найдено")
    meeting = _editable(db, user, a)
    if a.version != body.version:
        raise HTTPException(status.HTTP_409_CONFLICT, "Поручение изменено другим пользователем или системой")
    before = {"action": a.action, "assignee": a.assignee, "deadline": a.deadline, "review_state": a.review_state}
    if body.action is not None:
        a.action = body.action.strip()
    if body.assignee_name is not None or body.assignee_participant_id is not None:
        p = db.get(Participant, body.assignee_participant_id) if body.assignee_participant_id else None
        if p is not None and p.meeting_id != a.meeting_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Участник не из этой встречи")
        a.assignee = {"name": p.display_name if p else body.assignee_name, "participant_id": p.id if p else None,
                      "kind": "department" if body.assignee_type == "department" else "person",
                      "match_reason": "указано секретарём"}
        a.assignee_participant_id = p.id if p else None
        a.assignee_user_id = p.user_id if p else None
        a.assignee_type = body.assignee_type or ("participant" if p and p.is_present else "mentioned_person")
    if body.curator_name is not None:
        a.curator = {"name": body.curator_name, "participant_id": None} if body.curator_name else None
    if body.deadline_missing:
        a.deadline = {"kind": "missing", "raw_text": None, "normalized_date": None, "timezone": meeting.timezone,
                      "interpretation_notes": ["срок снят секретарём"], "policy_version": "manual"}
        a.deadline_confirmed = False
    elif body.deadline_date is not None:
        a.deadline = {**(a.deadline or {}), "kind": "date", "normalized_date": body.deadline_date.isoformat(),
                      "raw_text": body.deadline_raw or (a.deadline or {}).get("raw_text"), "timezone": meeting.timezone,
                      "ambiguous": False, "conflict": False,
                      "interpretation_notes": [*((a.deadline or {}).get("interpretation_notes") or []),
                                               "дата подтверждена/исправлена секретарём"]}
        a.deadline_confirmed = True
    if body.condition is not None:
        a.condition = body.condition or None
        a.conditional = bool(body.condition)
    if body.review_state is not None:
        a.review_state = body.review_state
        if body.review_state == "confirmed" and (a.deadline or {}).get("normalized_date") and \
                not (a.deadline or {}).get("ambiguous"):
            a.deadline_confirmed = True
    else:
        a.review_state = ReviewState.EDITED
    db.flush()
    n = (db.scalar(select(func.max(ActionRevision.number)).where(ActionRevision.action_id == a.id)) or 0) + 1
    db.add(ActionRevision(action_id=a.id, number=n, change_kind="edited", author_user_id=user.id,
                          snapshot={"action": a.action, "assignee": a.assignee, "deadline": a.deadline,
                                    "condition": a.condition, "review_state": a.review_state}))
    audit(db, action="action.edited", actor_user_id=user.id, meeting_id=a.meeting_id, object_type="action",
          object_id=a.id, before=before, after={"action": a.action, "assignee": a.assignee, "deadline": a.deadline,
                                                "review_state": a.review_state})
    from hattama.notifications.scheduler import reschedule

    reschedule(db, a)
    return action_out(db, a, user=user)


class ExecutionPatch(BaseModel):
    execution_state: Literal["open", "in_progress", "done", "cancelled"]


@router.post("/actions/{action_id}/execution")
def set_execution(action_id: str, body: ExecutionPatch, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    a = db.get(ActionItem, action_id)
    if a is None or not can_view_action(db, user, a):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Поручение не найдено")
    if not can_update_execution(db, user, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
    before = a.execution_state
    a.execution_state = body.execution_state
    audit(db, action="action.execution", actor_user_id=user.id, meeting_id=a.meeting_id, object_type="action",
          object_id=a.id, before={"execution_state": before}, after={"execution_state": body.execution_state})
    from hattama.notifications.scheduler import reschedule

    reschedule(db, a)
    return action_out(db, a, user=user)


@router.get("/actions")
def registry(scope: Literal["mine", "all"] = "all", overdue_only: bool = False, q: str | None = None,
             user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """Registry for managers and assignees: actions of meetings I am a member of, or assigned to me."""
    member_meetings = select(MeetingMember.meeting_id).where(MeetingMember.user_id == user.id)
    mine = or_(ActionItem.assignee_user_id == user.id,
               ActionItem.assignee_participant_id.in_(select(Participant.id).where(Participant.user_id == user.id)))
    stmt = select(ActionItem).where(ActionItem.review_state != ReviewState.REJECTED)
    stmt = stmt.where(mine) if scope == "mine" else stmt.where(or_(ActionItem.meeting_id.in_(member_meetings), mine))
    if q:
        stmt = stmt.where(ActionItem.action.ilike(f"%{q[:100]}%"))
    out = []
    for a in db.scalars(stmt.order_by(ActionItem.created_at.desc()).limit(500)):
        if a.meeting_id and db.get(Meeting, a.meeting_id).deleted_at is not None:  # type: ignore[union-attr]
            continue
        item = action_out(db, a, user=user)
        item["meeting_title"] = db.get(Meeting, a.meeting_id).title  # type: ignore[union-attr]
        if not overdue_only or item["overdue"]:
            out.append(item)
    return out


# ---------------------------------------------------------------- issues, summary, history
@router.get("/meetings/{meeting_id}/issues")
def issues(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    meeting_for(db, user, meeting_id, "read")
    return [{"id": i.id, "kind": i.kind, "severity": i.severity, "object_type": i.object_type,
             "object_id": i.object_id, "field": i.field, "question": i.question, "details": i.details,
             "status": i.status, "resolution": i.resolution}
            for i in db.scalars(select(ReviewIssue).where(ReviewIssue.meeting_id == meeting_id)
                                .order_by(ReviewIssue.status, ReviewIssue.created_at))]


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=1, max_length=2000)
    status: Literal["resolved", "dismissed"] = "resolved"


@router.post("/issues/{issue_id}/resolve")
def resolve_issue(issue_id: str, body: ResolveIn, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> dict[str, str]:
    issue = db.get(ReviewIssue, issue_id)
    if issue is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Вопрос не найден")
    meeting_for(db, user, issue.meeting_id, "edit")
    issue.status, issue.resolution, issue.resolved_by, issue.resolved_at = body.status, body.resolution, user.id, utcnow()
    audit(db, action="issue.resolved", actor_user_id=user.id, meeting_id=issue.meeting_id, object_type="issue",
          object_id=issue.id, after={"status": body.status, "resolution": body.resolution})
    return {"status": issue.status}


@router.get("/meetings/{meeting_id}/summary")
def summary(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    meeting = meeting_for(db, user, meeting_id, "read")
    s = db.get(SummaryRevision, meeting.active_summary_id) if meeting.active_summary_id else None
    return {"id": s.id, "number": s.number, "content": s.content, "origin": s.origin} if s else {"id": None}


@router.get("/meetings/{meeting_id}/history")
def history(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict]:
    meeting_for(db, user, meeting_id, "read")
    return [{"at": e.at, "action": e.action, "actor": e.actor_user_id, "actor_kind": e.actor_kind,
             "object_type": e.object_type, "object_id": e.object_id, "before": e.before, "after": e.after}
            for e in db.scalars(select(AuditEvent).where(AuditEvent.meeting_id == meeting_id)
                                .order_by(AuditEvent.id.desc()).limit(300))]


@router.post("/meetings/{meeting_id}/retry-analysis", status_code=202)
def retry_analysis(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    meeting = meeting_for(db, user, meeting_id, "edit")
    if meeting.active_transcript_revision_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Нет финальной расшифровки")
    job = enqueue(db, kind="extract_final", key=f"extract_final:{meeting_id}:{meeting.active_transcript_revision_id}:"
                                               f"retry:{int(utcnow().timestamp())}",
                  meeting_id=meeting_id, payload={"revision_id": meeting.active_transcript_revision_id})
    return {"job_id": job.id}


# ---------------------------------------------------------------- approval & exports
class ApproveIn(BaseModel):
    version: int


@router.post("/meetings/{meeting_id}/approve")
def approve(meeting_id: str, body: ApproveIn, user: User = Depends(current_user),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    meeting = meeting_for(db, user, meeting_id, "approve")
    if meeting.version != body.version:
        raise HTTPException(status.HTTP_409_CONFLICT, "Протокол изменился; обновите страницу перед утверждением")
    if meeting.status != MeetingStatus.NEEDS_REVIEW:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Утверждение недоступно в статусе {meeting.status}")
    blockers = db.scalar(select(func.count()).select_from(ReviewIssue).where(
        ReviewIssue.meeting_id == meeting_id, ReviewIssue.status == "open", ReviewIssue.severity == "blocker")) or 0
    if blockers:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Есть {blockers} нерешённых блокирующих вопросов")
    snap = build_snapshot(db, meeting)
    snap["meeting"]["status"] = MeetingStatus.APPROVED
    approval = ProtocolApproval(meeting_id=meeting_id, protocol_version=meeting.protocol_version, snapshot=snap,
                                snapshot_sha256=snapshot_hash(snap), approved_by=user.id)
    db.add(approval)
    meeting_status.transition(db, meeting_id, MeetingStatus.APPROVED, only_from={MeetingStatus.NEEDS_REVIEW})
    audit(db, action="protocol.approved", actor_user_id=user.id, meeting_id=meeting_id, object_type="meeting",
          object_id=meeting_id, after={"protocol_version": meeting.protocol_version, "sha256": approval.snapshot_sha256})
    return {"approval_id": approval.id, "protocol_version": meeting.protocol_version,
            "sha256": approval.snapshot_sha256}


@router.post("/meetings/{meeting_id}/new-version")
def new_version(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    meeting = meeting_for(db, user, meeting_id, "approve")
    if meeting.status != MeetingStatus.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Новая версия создаётся из утверждённого протокола")
    db.execute(Meeting.__table__.update().where(Meeting.id == meeting_id)
               .values(protocol_version=meeting.protocol_version + 1))
    meeting_status.transition(db, meeting_id, MeetingStatus.NEEDS_REVIEW, only_from={MeetingStatus.APPROVED})
    audit(db, action="protocol.new_version", actor_user_id=user.id, meeting_id=meeting_id, object_type="meeting",
          object_id=meeting_id, after={"protocol_version": meeting.protocol_version + 1})
    return {"protocol_version": meeting.protocol_version + 1}


class ExportIn(BaseModel):
    format: Literal["pdf", "docx"]


@router.post("/meetings/{meeting_id}/exports", status_code=201)
def create_export(meeting_id: str, body: ExportIn, user: User = Depends(current_user), db: Session = Depends(get_db),
                  settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    meeting = meeting_for(db, user, meeting_id, "read")
    approval = db.scalar(select(ProtocolApproval).where(ProtocolApproval.meeting_id == meeting_id,
                                                        ProtocolApproval.protocol_version == meeting.protocol_version))
    draft = meeting.status != MeetingStatus.APPROVED or approval is None
    snap = approval.snapshot if approval and not draft else build_snapshot(db, meeting)
    out_dir = settings.exports_dir / meeting_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
    name = f"protocol-v{meeting.protocol_version}-{'draft' if draft else 'approved'}-{stamp}.{body.format}"
    path = out_dir / name
    label = approval.snapshot_sha256[:12] if approval else None
    (render_pdf if body.format == "pdf" else render_docx)(snap, path, draft, label)
    import hashlib

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    art = ExportArtifact(meeting_id=meeting_id, protocol_version=meeting.protocol_version, format=body.format,
                         is_draft=draft, approval_id=approval.id if approval and not draft else None,
                         rel_path=str(path.relative_to(settings.data_dir)), sha256=digest,
                         size_bytes=path.stat().st_size, created_by=user.id)
    db.add(art)
    db.flush()
    audit(db, action="export.created", actor_user_id=user.id, meeting_id=meeting_id, object_type="export",
          object_id=art.id, after={"format": body.format, "draft": draft, "sha256": digest})
    return {"id": art.id, "format": body.format, "draft": draft, "sha256": digest,
            "download": f"/api/v1/exports/{art.id}/download"}


@router.get("/exports/{export_id}/download")
def download_export(export_id: str, user: User = Depends(current_user), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)) -> FileResponse:
    art = db.get(ExportArtifact, export_id)
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден")
    meeting_for(db, user, art.meeting_id, "read")
    path = settings.data_dir / art.rel_path
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "Файл удалён по политике хранения")
    media = "application/pdf" if art.format == "pdf" else \
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return FileResponse(path, media_type=media, filename=path.name)


# ---------------------------------------------------------------- audio playback (authorised, Range)
@router.get("/meetings/{meeting_id}/audio/{source_key}.wav")
def audio(meeting_id: str, source_key: str, request: Request, user: User = Depends(current_user),
          db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> Response:
    meeting_for(db, user, meeting_id, "read")
    from hattama.db.models import SourceEpoch

    row = db.execute(select(SourceEpoch).join(CaptureSource, CaptureSource.id == SourceEpoch.source_id)
                     .join(CaptureSession, CaptureSession.id == CaptureSource.capture_session_id)
                     .where(CaptureSession.meeting_id == meeting_id, CaptureSource.source_key == source_key)
                     .order_by(SourceEpoch.created_at).limit(1)).scalar()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Аудио не найдено")
    path = settings.data_dir / row.rel_path
    if not path.is_file():
        raise HTTPException(status.HTTP_410_GONE, "Аудио удалено по политике хранения")
    data_len = row.durable_samples * 2 * row.channel_count
    header = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, row.channel_count, row.sample_rate, row.sample_rate * 2 * row.channel_count,
        2 * row.channel_count, 16) + b"data" + struct.pack("<I", data_len)
    total = len(header) + data_len
    start, end = 0, total - 1
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes="):
        a, _, b = rng[6:].partition("-")
        start = int(a) if a else 0
        end = min(int(b), total - 1) if b else total - 1

    def body():  # type: ignore[no-untyped-def]
        pos = start
        if pos < len(header):
            chunk = header[pos:end + 1]
            yield chunk
            pos += len(chunk)
        with open(path, "rb") as fh:
            fh.seek(pos - len(header))
            while pos <= end:
                chunk = fh.read(min(65536, end - pos + 1))
                if not chunk:
                    yield b"\0" * (end - pos + 1)
                    break
                pos += len(chunk)
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    if rng:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return StreamingResponse(body(), status_code=206 if rng else 200, media_type="audio/wav", headers=headers)


# ---------------------------------------------------------------- notifications (internal only)
@router.get("/notifications")
def notifications(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id,
                                                 Notification.status.in_(["delivered", "read"]))
                      .order_by(Notification.scheduled_for.desc()).limit(100))
    return [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "action_id": n.action_id,
             "meeting_id": n.meeting_id, "status": n.status, "at": n.scheduled_for} for n in rows]


@router.post("/notifications/{notification_id}/read", status_code=204)
def read_notification(notification_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> None:
    n = db.get(Notification, notification_id)
    if n is None or n.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Не найдено")
    n.status, n.read_at = "read", utcnow()


@router.get("/me/role-in/{meeting_id}")
def my_role(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return {"role": member_role(db, meeting_id, user.id)}
