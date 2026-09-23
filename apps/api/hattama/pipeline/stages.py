"""Post-meeting pipeline stages (pipeline queue): finalize -> [final_asr on ASR worker] -> diarize ->
extract_final -> NEEDS_REVIEW. Each stage is idempotent (job keys, fingerprints, revision per job)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from hattama.config import Settings
from hattama.db.models import (
    ActionItem,
    CaptureSession,
    ExtractionRun,
    Meeting,
    Participant,
    Speaker,
    SpeakerBinding,
    SummaryRevision,
    TranscriptRevision,
    TranscriptSegment,
)
from hattama.db.session import session_scope
from hattama.db.types import utcnow
from hattama.domain import meeting_status
from hattama.domain.enums import MeetingStatus, ReviewState, RevisionKind, SourceKind
from hattama.domain.names import ParticipantRef
from hattama.extraction.evidence import SegmentView
from hattama.extraction.persist import add_issue, persist_cards
from hattama.extraction.pipeline import PIPELINE_VERSION, ExtractionPipeline, PipelineConfig
from hattama.jobs.queue import enqueue
from hattama.llm.client import LlamaCppClient
from hattama.runtime import active_profile
from hattama.services.events import audit, publish

log = logging.getLogger(__name__)
LIVE_WINDOW_SEGMENTS = 60


def llm_client(settings: Settings) -> LlamaCppClient:
    return LlamaCppClient(settings.llm_base_url, settings.llm_allowed_hosts, settings.llm_timeout_s)


def participants_of(session: Session, meeting_id: str) -> list[ParticipantRef]:
    return [ParticipantRef(p.id, p.display_name, tuple(p.aliases or ()), p.is_present, p.kind, p.user_id)
            for p in session.scalars(select(Participant).where(Participant.meeting_id == meeting_id))]


def segment_views(session: Session, revision_id: str, limit_last: int | None = None) -> list[SegmentView]:
    segs = list(session.scalars(select(TranscriptSegment).where(TranscriptSegment.revision_id == revision_id)
                                .order_by(TranscriptSegment.start_ms)))
    if limit_last:
        segs = segs[-limit_last:]
    speakers = {s.id: s for s in session.scalars(select(Speaker).where(Speaker.revision_id == revision_id))}
    bindings: dict[str, Participant] = {}
    for b in session.scalars(select(SpeakerBinding).where(SpeakerBinding.status == "confirmed")):
        p = session.get(Participant, b.participant_id)
        if p is not None:
            bindings[b.speaker_id] = p
    views = []
    for i, seg in enumerate(segs):
        spk = speakers.get(seg.speaker_id or "")
        bound = bindings.get(seg.speaker_id or "")
        # neutral label: a source name like "Звук встречи" must not look like a person to the LLM
        label = "Говорящий не определён" if not bound else spk.label if spk else "Говорящий не определён"
        views.append(SegmentView(id=seg.id, alias=f"S{i + 1}", text=seg.text, start_ms=seg.start_ms, end_ms=seg.end_ms,
                                 revision_id=revision_id, speaker_label=label,
                                 speaker_name=bound.display_name if bound else None,
                                 speaker_participant_id=bound.id if bound else None, words=seg.words or [], order=i))
    return views


# ------------------------------------------------------------------ stages
def stage_finalize(session: Session, meeting_id: str, payload: dict[str, Any]) -> dict:
    enqueue(session, kind="final_asr", key=f"final_asr:{meeting_id}:{payload.get('capture_session_id')}",
            queue="asr", meeting_id=meeting_id, payload=payload, priority=50)
    publish(session, meeting_id, "job.state", {"kind": "finalize", "state": "final_asr_queued"})
    return {"queued": "final_asr"}


def stage_diarize(settings: Settings, meeting_id: str, payload: dict[str, Any]) -> dict:
    """Speaker turns. Separate sources give speakers directly (own microphone vs meeting audio).
    Mixed meeting audio needs pyannote (local, gated weights); if unavailable this is SAID, not hidden."""
    revision_id = payload["revision_id"]
    from hattama.diarization.provider import DiarizationUnavailableError, diarize_revision

    with session_scope() as s:
        by_source: dict[str, Speaker] = {}
        for seg in s.scalars(select(TranscriptSegment).where(TranscriptSegment.revision_id == revision_id)):
            if seg.source_key not in by_source:
                sp = Speaker(meeting_id=meeting_id, revision_id=revision_id, source_key=seg.source_key,
                             label="Микрофон (вы)" if seg.source_key in ("mic",) else "Звук встречи")
                s.add(sp)
                s.flush()
                by_source[seg.source_key] = sp
                me = s.scalar(select(Participant).where(Participant.meeting_id == meeting_id,
                                                        Participant.is_self.is_(True)))
                cs = s.get(CaptureSession, seg.capture_session_id) if seg.capture_session_id else None
                if me is not None and seg.source_key == "mic" and cs is not None:
                    s.add(SpeakerBinding(speaker_id=sp.id, participant_id=me.id, status="suggested",
                                         reason="отдельный источник: собственный микрофон секретаря"))
            seg.speaker_id = by_source[seg.source_key].id
    result: dict[str, Any] = {"mode": "per_source"}
    try:
        result = diarize_revision(settings, meeting_id, revision_id)
    except DiarizationUnavailableError as exc:
        with session_scope() as s:
            add_issue(s, meeting_id, f"diarization_unavailable:{revision_id}", "diarization_unavailable", "warning",
                      f"Диаризация смешанного звука встречи не выполнена: {exc}. Говорящие размечены только по "
                      "источнику (микрофон / звук встречи); назначьте говорящих вручную.")
        result["diarization"] = f"unavailable: {exc}"
    with session_scope() as s:
        activate_final_revision(s, meeting_id, revision_id)
        enqueue(s, kind="extract_final", key=f"extract_final:{meeting_id}:{revision_id}", meeting_id=meeting_id,
                payload={"revision_id": revision_id}, priority=70)
    return result


def activate_final_revision(session: Session, meeting_id: str, revision_id: str) -> None:
    meeting = session.get(Meeting, meeting_id)
    assert meeting is not None
    current = meeting.active_transcript_revision_id
    edited = False
    if current and current != revision_id:
        edited = session.scalar(select(TranscriptSegment.id).where(TranscriptSegment.revision_id == current,
                                                                   TranscriptSegment.edited_by.is_not(None))
                                .limit(1)) is not None
    if edited:
        add_issue(session, meeting_id, f"final_vs_manual:{revision_id}", "final_revision_available", "info",
                  "Финальная расшифровка готова, но в текущей версии есть ручные правки. Сравните версии и "
                  "выберите активную.", "transcript", revision_id)
        return
    session.execute(update(Meeting).where(Meeting.id == meeting_id)
                    .values(active_transcript_revision_id=revision_id, version=Meeting.version + 1))


def stage_extract(settings: Settings, meeting_id: str, payload: dict[str, Any], *, live: bool) -> dict:
    with session_scope() as s:
        meeting = s.get(Meeting, meeting_id)
        assert meeting is not None
        if live:
            rev = s.scalar(select(TranscriptRevision).where(TranscriptRevision.meeting_id == meeting_id,
                                                            TranscriptRevision.kind == RevisionKind.LIVE))
            if rev is None:
                return {"skipped": "no live transcript"}
            revision_id = rev.id
        else:
            revision_id = payload["revision_id"]
        views = segment_views(s, revision_id, LIVE_WINDOW_SEGMENTS if live else None)
        parts = participants_of(s, meeting_id)
        mdate, tz, title = meeting.meeting_date, meeting.timezone, meeting.title
        key = f"{'live' if live else 'final'}:{revision_id}:{PIPELINE_VERSION}:{len(views)}"
        existing = s.scalar(select(ExtractionRun).where(ExtractionRun.meeting_id == meeting_id,
                                                        ExtractionRun.idempotency_key == key))
        if existing is not None and existing.status == "succeeded":
            return {"skipped": "already extracted", "run_id": existing.id}
        if existing is None:
            existing = ExtractionRun(meeting_id=meeting_id, kind="live" if live else "final", idempotency_key=key,
                                     transcript_revision_id=revision_id)
            s.add(existing)
            s.flush()
        run_id = existing.id
    if not views:
        with session_scope() as s:
            s.get(ExtractionRun, run_id).status = "succeeded"  # type: ignore[union-attr]
            if not live:
                _mark_ready(s, meeting_id, "пустая расшифровка")
        return {"segments": 0}
    client = llm_client(settings)
    profile = active_profile(settings)
    cfg = PipelineConfig(ctx_size=int(profile["llm"]["ctx_size"]), with_summary=not live)
    try:
        result = ExtractionPipeline(client, cfg).run(meeting_date=mdate, timezone=tz, title=title, participants=parts,
                                                     ordered=views)
    finally:
        client.close()
    with session_scope() as s:
        run = s.get(ExtractionRun, run_id)
        assert run is not None
        stats = persist_cards(s, meeting_id, run_id, "live" if live else "final", result.cards)
        run.status = "succeeded"
        run.finished_at = utcnow()
        run.model = client.base_url
        run.stats = {"persist": stats, "llm_seconds": result.llm_seconds, "windows": len(result.stats),
                     "rejected_events": result.rejected_events, "window_errors": result.window_errors,
                     "raw_events": result.raw_events}
        for err in result.window_errors:
            add_issue(s, meeting_id, f"window_error:{run_id}:{err['window']}:{err['kind']}", "extraction_window_failed",
                      "warning", f"Фрагмент расшифровки не обработан моделью ({err['error'][:200]}). Проверьте "
                      "его вручную.", details=err)
        if not live:
            # preliminary live cards not touched by a human are superseded by the final pass
            for item in s.scalars(select(ActionItem).where(ActionItem.meeting_id == meeting_id,
                                                           ActionItem.origin == "live",
                                                           ActionItem.review_state.notin_([ReviewState.EDITED,
                                                                                           ReviewState.CONFIRMED]))):
                if item.extraction_run_id != run_id:
                    item.review_state = ReviewState.REJECTED
                    item.review_reasons = [*item.review_reasons, "заменено финальным проходом"]
            from sqlalchemy import func

            number = (s.scalar(select(func.max(SummaryRevision.number))
                               .where(SummaryRevision.meeting_id == meeting_id)) or 0) + 1
            summ = SummaryRevision(meeting_id=meeting_id, number=number, content=result.summary, origin="machine",
                                   transcript_revision_id=revision_id)
            s.add(summ)
            s.flush()
            meeting = s.get(Meeting, meeting_id)
            if meeting is not None and meeting.active_summary_id is None:
                s.execute(update(Meeting).where(Meeting.id == meeting_id)
                          .values(active_summary_id=summ.id, version=Meeting.version + 1))
            _mark_ready(s, meeting_id, "извлечение завершено")
    return {"cards": len(result.cards), **stats, "llm_seconds": result.llm_seconds}


def _mark_ready(session: Session, meeting_id: str, why: str) -> None:
    if meeting_status.transition(session, meeting_id, MeetingStatus.NEEDS_REVIEW):
        publish(session, meeting_id, "meeting.status", {"status": MeetingStatus.NEEDS_REVIEW, "reason": why})
        audit(session, action="meeting.ready_for_review", actor_user_id=None, actor_kind="system",
              meeting_id=meeting_id, after={"reason": why})


def mark_llm_failed(meeting_id: str, error: str) -> None:
    """After bounded retries: transcript is still reviewable; the missing analysis is an explicit issue."""
    with session_scope() as s:
        add_issue(s, meeting_id, "llm_unavailable", "llm_unavailable", "blocker",
                  f"Локальная LLM недоступна, поручения и саммари не извлечены: {error}. Запустите llama-server "
                  "(python tasks.py llm) и нажмите «Повторить анализ».")
        _mark_ready(s, meeting_id, "LLM недоступна")


__all__ = ["SourceKind", "stage_diarize", "stage_extract", "stage_finalize"]
