"""ORM entities. Business rules live in hattama.domain / services, not here."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from hattama.db.types import UTCDateTime, new_id, utcnow


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _id() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


# ------------------------------------------------------------------ identity & access
class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = _id()
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = _id()
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


# ------------------------------------------------------------------ meetings
class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[str] = _id()
    title: Mapped[str] = mapped_column(String(300))
    meeting_date: Mapped[date] = mapped_column(Date)
    start_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # HH:MM local
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Almaty")
    language_mode: Mapped[str] = mapped_column(String(10), default="mixed")
    platform: Mapped[str] = mapped_column(String(20), default="google_meet")
    meeting_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    consent_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    consent_confirmed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    protocol_version: Mapped[int] = mapped_column(Integer, default=1)
    active_transcript_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active_summary_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __mapper_args__ = {"version_id_col": version}

    participants: Mapped[list[Participant]] = relationship(back_populates="meeting", cascade="all, delete-orphan",
                                                           order_by="Participant.created_at")
    members: Mapped[list[MeetingMember]] = relationship(back_populates="meeting", cascade="all, delete-orphan")


class MeetingMember(Base):
    __tablename__ = "meeting_members"
    __table_args__ = (UniqueConstraint("meeting_id", "user_id"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    meeting: Mapped[Meeting] = relationship(back_populates="members")


class Participant(Base):
    __tablename__ = "participants"
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    position: Mapped[str | None] = mapped_column(String(200), nullable=True)
    department: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="person")  # person | department
    is_present: Mapped[bool] = mapped_column(Boolean, default=True)
    is_self: Mapped[bool] = mapped_column(Boolean, default=False)  # the person capturing (own microphone)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    meeting: Mapped[Meeting] = relationship(back_populates="participants")


# ------------------------------------------------------------------ capture & audio
class CaptureSession(Base):
    __tablename__ = "capture_sessions"
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="CREATED", index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timeline_origin_us: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    client_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_frame_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    connected: Mapped[bool] = mapped_column(Boolean, default=False)


class PairingCode(Base):
    __tablename__ = "pairing_codes"
    id: Mapped[str] = _id()
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class IngestToken(Base):
    __tablename__ = "ingest_tokens"
    id: Mapped[str] = _id()
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"),
                                                    index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    scope: Mapped[str] = mapped_column(String(20), default="ingest")
    client_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class CaptureSource(Base):
    __tablename__ = "capture_sources"
    __table_args__ = (UniqueConstraint("capture_session_id", "source_key"),
                      UniqueConstraint("capture_session_id", "source_index"))
    id: Mapped[str] = _id()
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"),
                                                    index=True)
    source_key: Mapped[str] = mapped_column(String(32))
    source_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(120), default="")
    last_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SourceEpoch(Base):
    __tablename__ = "source_epochs"
    __table_args__ = (UniqueConstraint("source_id", "epoch"),)
    id: Mapped[str] = _id()
    source_id: Mapped[str] = mapped_column(ForeignKey("capture_sources.id", ondelete="CASCADE"), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    sample_rate: Mapped[int] = mapped_column(Integer)
    channel_count: Mapped[int] = mapped_column(Integer)
    epoch_start_wall_us: Mapped[int] = mapped_column(BigInteger)
    rel_path: Mapped[str] = mapped_column(String(500))
    durable_sequence: Mapped[int] = mapped_column(BigInteger, default=-1)
    durable_samples: Mapped[int] = mapped_column(BigInteger, default=0)
    received_frames: Mapped[int] = mapped_column(BigInteger, default=0)
    duplicate_frames: Mapped[int] = mapped_column(BigInteger, default=0)
    live_processed_samples: Mapped[int] = mapped_column(BigInteger, default=0)
    live_done: Mapped[bool] = mapped_column(Boolean, default=False)
    final_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AudioGap(Base):
    __tablename__ = "audio_gaps"
    id: Mapped[str] = _id()
    source_epoch_id: Mapped[str] = mapped_column(ForeignKey("source_epochs.id", ondelete="CASCADE"), index=True)
    start_sample: Mapped[int] = mapped_column(BigInteger)
    end_sample: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(40))
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AudioAsset(Base):
    __tablename__ = "audio_assets"
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # upload_original | upload_decoded | playback
    rel_path: Mapped[str] = mapped_column(String(500))
    sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capture_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ------------------------------------------------------------------ transcript & speakers
class TranscriptRevision(Base):
    __tablename__ = "transcript_revisions"
    __table_args__ = (UniqueConstraint("meeting_id", "number"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12), default="building")
    base_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    asr_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    asr_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Speaker(Base):
    __tablename__ = "speakers"
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    label: Mapped[str] = mapped_column(String(64))  # SPEAKER_00, mic:self, ...
    source_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merged_into_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sample_segment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    total_speech_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SpeakerBinding(Base):
    __tablename__ = "speaker_bindings"
    id: Mapped[str] = _id()
    speaker_id: Mapped[str] = mapped_column(ForeignKey("speakers.id", ondelete="CASCADE"), index=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(12))  # suggested | confirmed | rejected
    reason: Mapped[str] = mapped_column(String(300))
    evidence_segment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    __table_args__ = (Index("ix_segments_rev_order", "revision_id", "start_ms"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("transcript_revisions.id", ondelete="CASCADE"))
    capture_session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_key: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer, default=0)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    original_text: Mapped[str] = mapped_column(Text)
    words: Mapped[list[Any]] = mapped_column(JSON, default=list)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    speaker_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(10))
    rev: Mapped[int] = mapped_column(Integer, default=1)
    overlap: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    avg_logprob: Mapped[float | None] = mapped_column(Float, nullable=True)
    edited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __mapper_args__ = {"version_id_col": rev}


# ------------------------------------------------------------------ actions, evidence, review
class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (UniqueConstraint("meeting_id", "idempotency_key"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # live | final
    idempotency_key: Mapped[str] = mapped_column(String(128))
    transcript_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="running")
    windows_total: Mapped[int] = mapped_column(Integer, default=0)
    windows_done: Mapped[int] = mapped_column(Integer, default=0)
    last_segment_seq: Mapped[int] = mapped_column(Integer, default=-1)
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ActionItem(Base):
    __tablename__ = "action_items"
    __table_args__ = (UniqueConstraint("meeting_id", "fingerprint"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str] = mapped_column(String(10), default="final")  # live | final | manual
    action: Mapped[str] = mapped_column(Text)
    expected_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigner: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    assignee: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    assignee_type: Mapped[str] = mapped_column(String(20), default="unknown")
    assignee_participant_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    assignee_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    curator: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    collaborators: Mapped[list[Any]] = mapped_column(JSON, default=list)
    deadline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    deadline_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    condition: Mapped[str | None] = mapped_column(Text, nullable=True)
    conditional: Mapped[bool] = mapped_column(Boolean, default=False)
    dependencies: Mapped[list[Any]] = mapped_column(JSON, default=list)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(300), nullable=True)
    review_state: Mapped[str] = mapped_column(String(15), default="proposed")
    execution_state: Mapped[str] = mapped_column(String(15), default="open")
    on_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    extraction_run_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    local_ref: Mapped[str | None] = mapped_column(String(16), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __mapper_args__ = {"version_id_col": version}


class ActionRevision(Base):
    __tablename__ = "action_revisions"
    __table_args__ = (UniqueConstraint("action_id", "number"),)
    id: Mapped[str] = _id()
    action_id: Mapped[str] = mapped_column(ForeignKey("action_items.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    change_kind: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    event: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    author_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class EvidenceSpan(Base):
    __tablename__ = "evidence_spans"
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("action_items.id", ondelete="CASCADE"), nullable=True,
                                                  index=True)
    summary_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary_item_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    field: Mapped[str] = mapped_column(String(20))
    segment_id: Mapped[str] = mapped_column(String(32))
    transcript_revision_id: Mapped[str] = mapped_column(String(32))
    quote: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    end_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    match: Mapped[str] = mapped_column(String(10))  # exact | fuzzy | missing
    superseded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ReviewIssue(Base):
    __tablename__ = "review_issues"
    __table_args__ = (UniqueConstraint("meeting_id", "dedupe_key"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))
    object_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    field: Mapped[str | None] = mapped_column(String(30), nullable=True)
    question: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="open")
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SummaryRevision(Base):
    __tablename__ = "summary_revisions"
    __table_args__ = (UniqueConstraint("meeting_id", "number"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    origin: Mapped[str] = mapped_column(String(10))  # machine | manual
    transcript_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ProtocolApproval(Base):
    __tablename__ = "protocol_approvals"
    __table_args__ = (UniqueConstraint("meeting_id", "protocol_version"),)
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    protocol_version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ExportArtifact(Base):
    __tablename__ = "export_artifacts"
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    protocol_version: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(5))
    is_draft: Mapped[bool] = mapped_column(Boolean)
    approval_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rel_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ------------------------------------------------------------------ notifications, audit, jobs, events
class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("dedupe_key"),)
    id: Mapped[str] = _id()
    dedupe_key: Mapped[str] = mapped_column(String(200))
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    meeting_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    action_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20))
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    status: Mapped[str] = mapped_column(String(12), default="scheduled", index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_object", "object_type", "object_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_kind: Mapped[str] = mapped_column(String(12))  # user | system | extension | agent
    action: Mapped[str] = mapped_column(String(60))
    object_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("key"), Index("ix_jobs_claim", "state", "queue", "run_after"))
    id: Mapped[str] = _id()
    key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))
    queue: Mapped[str] = mapped_column(String(20), default="pipeline")  # pipeline | asr
    meeting_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(12), default="queued")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    run_after: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LiveEvent(Base):
    __tablename__ = "live_events"
    __table_args__ = (Index("ix_live_events_meeting", "meeting_id", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str] = mapped_column(String(32))
    type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MetricSample(Base):
    __tablename__ = "metric_samples"
    __table_args__ = (Index("ix_metrics_meeting_name", "meeting_id", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(50))
    value: Mapped[float] = mapped_column(Float)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class BotSession(Base):
    __tablename__ = "bot_sessions"
    id: Mapped[str] = _id()
    meeting_id: Mapped[str] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    capture_session_id: Mapped[str] = mapped_column(ForeignKey("capture_sessions.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(20))
    meeting_url: Mapped[str] = mapped_column(String(2000))
    display_name: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(32), default="CREATED")
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    recording_approved_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    recording_approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    admitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    diagnostics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ResourceLock(Base):
    __tablename__ = "resource_locks"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
