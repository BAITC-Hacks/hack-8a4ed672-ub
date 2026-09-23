"""One server-side readiness policy shared by the UI and protocol approval."""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hattama.db.models import (
    CaptureSession,
    ExtractionRun,
    Job,
    Meeting,
    ReviewIssue,
    SummaryRevision,
    TranscriptRevision,
    TranscriptSegment,
)
from hattama.domain.enums import CaptureState, MeetingStatus

FINAL_JOB_KINDS = ("finalize_meeting", "final_asr", "diarize", "extract_final")


def meaningful_summary(content: Any) -> bool:
    if not isinstance(content, dict):
        return False
    return any(
        isinstance(item, dict) and isinstance(item.get("text"), str) and bool(item["text"].strip())
        for key in ("facts", "decisions", "assumptions", "risks", "open_questions")
        for item in (content.get(key) if isinstance(content.get(key), list) else [])
    )


def approval_readiness(db: Session, meeting: Meeting) -> dict[str, Any]:
    revision = db.get(TranscriptRevision, meeting.active_transcript_revision_id) \
        if meeting.active_transcript_revision_id else None
    transcript_ready = bool(revision and revision.meeting_id == meeting.id and revision.kind != "live"
                            and revision.status == "complete" and db.scalar(select(TranscriptSegment.id).where(
                                TranscriptSegment.revision_id == revision.id,
                                func.length(func.trim(TranscriptSegment.text)) > 0).limit(1)))
    summary = db.get(SummaryRevision, meeting.active_summary_id) if meeting.active_summary_id else None
    summary_ready = bool(summary and summary.meeting_id == meeting.id
                         and summary.transcript_revision_id == meeting.active_transcript_revision_id
                         and meaningful_summary(summary.content))
    run = db.scalar(select(ExtractionRun).where(
        ExtractionRun.meeting_id == meeting.id, ExtractionRun.kind == "final",
        ExtractionRun.transcript_revision_id == meeting.active_transcript_revision_id
    ).order_by(ExtractionRun.started_at.desc()).limit(1))
    analysis_ready = bool(run and run.status == "succeeded" and not (run.stats or {}).get("window_errors"))
    captures = db.scalar(select(CaptureSession.id).where(
        CaptureSession.meeting_id == meeting.id,
        CaptureSession.state.in_([CaptureState.CREATED, CaptureState.ACTIVE, CaptureState.STOPPING])).limit(1))
    jobs = list(db.scalars(select(Job).where(Job.meeting_id == meeting.id, Job.kind.in_(FINAL_JOB_KINDS))
                           .order_by(Job.created_at.desc()).limit(50)))
    pending = any(j.state in ("queued", "running") and not j.cancel_requested for j in jobs)
    blockers = db.scalar(select(func.count()).select_from(ReviewIssue).where(
        ReviewIssue.meeting_id == meeting.id, ReviewIssue.status == "open", ReviewIssue.severity == "blocker")) or 0
    reasons = []
    if meeting.status != MeetingStatus.NEEDS_REVIEW:
        reasons.append("Утверждение доступно после завершения обработки и проверки протокола")
    if captures:
        reasons.append("Сначала завершите запись")
    if pending:
        reasons.append("Обработка записи ещё выполняется")
    if not transcript_ready:
        reasons.append("Нет готовой расшифровки с распознанной речью")
    if not summary_ready:
        reasons.append("Нет готовых итогов для текущей расшифровки")
    if not analysis_ready:
        reasons.append("Анализ текущей расшифровки ещё не завершён успешно")
    if blockers:
        reasons.append(f"Есть {blockers} нерешённых блокирующих вопросов")
    return {"can_approve": not reasons, "reasons": reasons,
            "processing": pending or meeting.status == MeetingStatus.PROCESSING,
            "transcript_ready": transcript_ready, "summary_ready": summary_ready, "analysis_ready": analysis_ready,
            "jobs": [{"id": j.id, "kind": j.kind, "status": j.state, "error": j.last_error,
                      "progress": j.progress, "updated_at": j.updated_at} for j in jobs]}
