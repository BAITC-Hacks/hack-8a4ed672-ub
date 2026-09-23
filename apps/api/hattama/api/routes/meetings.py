from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hattama.api.deps import current_user, get_db, meeting_for, member_role
from hattama.db.models import (
    ActionItem,
    CaptureSession,
    Meeting,
    MeetingMember,
    Participant,
    ReviewIssue,
    User,
)
from hattama.db.types import utcnow
from hattama.domain import meeting_status
from hattama.domain.enums import MeetingStatus, MemberRole, UserRole
from hattama.security.urls import MeetingUrlError, validate_meeting_url
from hattama.services.events import audit, publish

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])

DEFAULT_CONSENT_TEXT = (
    "Участники совещания уведомлены, что встреча записывается и расшифровывается локальной системой "
    "«Хаттама Live» для подготовки протокола. Записи и тексты не передаются во внешние сервисы ИИ."
)


class ParticipantIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    position: str | None = Field(default=None, max_length=200)
    department: str | None = Field(default=None, max_length=200)
    kind: Literal["person", "department"] = "person"
    is_present: bool = True
    is_self: bool = False
    aliases: list[str] = Field(default_factory=list, max_length=10)
    user_email: str | None = Field(default=None, max_length=254)


class ParticipantOut(BaseModel):
    id: str
    display_name: str
    position: str | None
    department: str | None
    kind: str
    is_present: bool
    is_self: bool
    aliases: list[str]
    user_id: str | None
    notified_at: datetime | None


class MeetingIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    meeting_date: date
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    timezone: str = "Asia/Almaty"
    language_mode: Literal["mixed", "ru", "kk", "auto"] = "mixed"
    platform: Literal["google_meet", "teams_web", "zoom_web", "in_person", "other"] = "google_meet"
    meeting_url: str | None = Field(default=None, max_length=2000)
    participants: list[ParticipantIn] = Field(default_factory=list, max_length=200)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Неизвестный часовой пояс") from exc
        return value


class MeetingPatch(BaseModel):
    version: int
    title: str | None = Field(default=None, min_length=1, max_length=300)
    meeting_date: date | None = None
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    timezone: str | None = None
    language_mode: Literal["mixed", "ru", "kk", "auto"] | None = None
    platform: Literal["google_meet", "teams_web", "zoom_web", "in_person", "other"] | None = None
    meeting_url: str | None = None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        if value is not None:
            ZoneInfo(value)
        return value


class ConsentIn(BaseModel):
    confirmed: bool
    notice_text: str = Field(default=DEFAULT_CONSENT_TEXT, min_length=20, max_length=2000)


class MeetingSummaryOut(BaseModel):
    id: str
    title: str
    meeting_date: date
    start_time: str | None
    timezone: str
    status: str
    platform: str
    my_role: str | None
    open_issues: int
    actions: int
    updated_at: datetime


class MeetingOut(BaseModel):
    id: str
    title: str
    meeting_date: date
    start_time: str | None
    timezone: str
    language_mode: str
    platform: str
    meeting_url: str | None
    status: str
    version: int
    protocol_version: int
    consent_text: str | None
    consent_confirmed_at: datetime | None
    my_role: str | None
    participants: list[ParticipantOut]
    active_transcript_revision_id: str | None
    default_consent_text: str = DEFAULT_CONSENT_TEXT


def participant_out(p: Participant) -> ParticipantOut:
    return ParticipantOut(id=p.id, display_name=p.display_name, position=p.position, department=p.department,
                          kind=p.kind, is_present=p.is_present, is_self=p.is_self, aliases=list(p.aliases or []),
                          user_id=p.user_id, notified_at=p.notified_at)


def meeting_out(db: Session, meeting: Meeting, user: User) -> MeetingOut:
    return MeetingOut(
        id=meeting.id, title=meeting.title, meeting_date=meeting.meeting_date, start_time=meeting.start_time,
        timezone=meeting.timezone, language_mode=meeting.language_mode, platform=meeting.platform,
        meeting_url=meeting.meeting_url, status=meeting.status, version=meeting.version,
        protocol_version=meeting.protocol_version, consent_text=meeting.consent_text,
        consent_confirmed_at=meeting.consent_confirmed_at, my_role=member_role(db, meeting.id, user.id),
        participants=[participant_out(p) for p in meeting.participants],
        active_transcript_revision_id=meeting.active_transcript_revision_id,
    )


def _check_url(platform: str, url: str | None) -> str | None:
    if not url:
        return None
    try:
        return validate_meeting_url(url, platform)
    except MeetingUrlError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _link_user(db: Session, email: str | None) -> str | None:
    if not email:
        return None
    user = db.scalar(select(User).where(func.lower(User.email) == email.lower()))
    return user.id if user else None


@router.get("", response_model=list[MeetingSummaryOut])
def list_meetings(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[MeetingSummaryOut]:
    rows = db.execute(
        select(Meeting, MeetingMember.role)
        .join(MeetingMember, MeetingMember.meeting_id == Meeting.id)
        .where(MeetingMember.user_id == user.id, Meeting.deleted_at.is_(None))
        .order_by(Meeting.meeting_date.desc(), Meeting.created_at.desc())
    ).all()
    out = []
    for meeting, role in rows:
        issues = db.scalar(select(func.count()).select_from(ReviewIssue)
                           .where(ReviewIssue.meeting_id == meeting.id, ReviewIssue.status == "open")) or 0
        actions = db.scalar(select(func.count()).select_from(ActionItem)
                            .where(ActionItem.meeting_id == meeting.id, ActionItem.review_state != "rejected")) or 0
        out.append(MeetingSummaryOut(id=meeting.id, title=meeting.title, meeting_date=meeting.meeting_date,
                                     start_time=meeting.start_time, timezone=meeting.timezone, status=meeting.status,
                                     platform=meeting.platform, my_role=role, open_issues=issues, actions=actions,
                                     updated_at=meeting.updated_at))
    return out


@router.post("", response_model=MeetingOut, status_code=201)
def create_meeting(body: MeetingIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeetingOut:
    if user.role not in (UserRole.SECRETARY, UserRole.ADMIN):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Создавать встречи может секретарь")
    meeting = Meeting(title=body.title, meeting_date=body.meeting_date, start_time=body.start_time,
                      timezone=body.timezone, language_mode=body.language_mode, platform=body.platform,
                      meeting_url=_check_url(body.platform, body.meeting_url), owner_id=user.id,
                      status=MeetingStatus.DRAFT)
    db.add(meeting)
    db.flush()
    db.add(MeetingMember(meeting_id=meeting.id, user_id=user.id, role=MemberRole.SECRETARY))
    for p in body.participants:
        db.add(Participant(meeting_id=meeting.id, display_name=p.display_name.strip(), position=p.position,
                           department=p.department, kind=p.kind, is_present=p.is_present, is_self=p.is_self,
                           aliases=p.aliases, user_id=_link_user(db, p.user_email)))
    audit(db, action="meeting.created", actor_user_id=user.id, meeting_id=meeting.id, object_type="meeting",
          object_id=meeting.id, after={"title": meeting.title, "date": str(meeting.meeting_date)})
    db.flush()
    db.refresh(meeting)
    return meeting_out(db, meeting, user)


@router.get("/{meeting_id}", response_model=MeetingOut)
def get_meeting(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeetingOut:
    return meeting_out(db, meeting_for(db, user, meeting_id, "read"), user)


@router.patch("/{meeting_id}", response_model=MeetingOut)
def patch_meeting(meeting_id: str, body: MeetingPatch, user: User = Depends(current_user),
                  db: Session = Depends(get_db)) -> MeetingOut:
    meeting = meeting_for(db, user, meeting_id, "edit")
    if meeting.version != body.version:
        raise HTTPException(status.HTTP_409_CONFLICT, "Встреча изменена другим пользователем; обновите страницу")
    if meeting.status == MeetingStatus.APPROVED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Протокол утверждён; создайте новую версию")
    before = {"title": meeting.title, "meeting_date": str(meeting.meeting_date), "timezone": meeting.timezone}
    changes = body.model_dump(exclude_unset=True, exclude={"version"})
    if "meeting_url" in changes:
        changes["meeting_url"] = _check_url(changes.get("platform") or meeting.platform, changes["meeting_url"])
    for key, value in changes.items():
        setattr(meeting, key, value)
    audit(db, action="meeting.updated", actor_user_id=user.id, meeting_id=meeting.id, object_type="meeting",
          object_id=meeting.id, before=before, after={k: str(v) for k, v in changes.items()})
    db.flush()
    return meeting_out(db, meeting, user)


@router.post("/{meeting_id}/consent", response_model=MeetingOut)
def confirm_consent(meeting_id: str, body: ConsentIn, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> MeetingOut:
    meeting = meeting_for(db, user, meeting_id, "capture")
    if not body.confirmed:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Без уведомления участников запись невозможна")
    meeting.consent_text = body.notice_text
    meeting.consent_confirmed_at = utcnow()
    meeting.consent_confirmed_by = user.id
    now = utcnow()
    for p in meeting.participants:
        if p.is_present and p.notified_at is None:
            p.notified_at = now
    db.flush()
    meeting_status.transition(db, meeting.id, MeetingStatus.READY, only_from={MeetingStatus.DRAFT})
    audit(db, action="meeting.consent_confirmed", actor_user_id=user.id, meeting_id=meeting.id,
          object_type="meeting", object_id=meeting.id, after={"notice_text": body.notice_text})
    db.expire(meeting)
    return meeting_out(db, meeting, user)


@router.post("/{meeting_id}/participants", response_model=ParticipantOut, status_code=201)
def add_participant(meeting_id: str, body: ParticipantIn, user: User = Depends(current_user),
                    db: Session = Depends(get_db)) -> ParticipantOut:
    meeting = meeting_for(db, user, meeting_id, "edit")
    live = meeting.status == MeetingStatus.LIVE
    p = Participant(meeting_id=meeting.id, display_name=body.display_name.strip(), position=body.position,
                    department=body.department, kind=body.kind, is_present=body.is_present, is_self=body.is_self,
                    aliases=body.aliases, user_id=_link_user(db, body.user_email))
    db.add(p)
    db.flush()
    audit(db, action="participant.added", actor_user_id=user.id, meeting_id=meeting.id, object_type="participant",
          object_id=p.id, after={"display_name": p.display_name, "during_recording": live})
    if live and p.is_present:
        # A participant who joins during recording must be notified per policy; the secretary confirms it.
        publish(db, meeting.id, "issue.created", {"kind": "participant_notice_required", "participant_id": p.id,
                                                  "display_name": p.display_name})
    return participant_out(p)


class ParticipantPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    position: str | None = None
    department: str | None = None
    is_present: bool | None = None
    is_self: bool | None = None
    aliases: list[str] | None = Field(default=None, max_length=10)
    user_email: str | None = None


@router.patch("/{meeting_id}/participants/{participant_id}", response_model=ParticipantOut)
def patch_participant(meeting_id: str, participant_id: str, body: ParticipantPatch,
                      user: User = Depends(current_user), db: Session = Depends(get_db)) -> ParticipantOut:
    meeting_for(db, user, meeting_id, "edit")
    p = db.get(Participant, participant_id)
    if p is None or p.meeting_id != meeting_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Участник не найден")
    changes = body.model_dump(exclude_unset=True)
    if "user_email" in changes:
        p.user_id = _link_user(db, changes.pop("user_email"))
    for key, value in changes.items():
        setattr(p, key, value)
    audit(db, action="participant.updated", actor_user_id=user.id, meeting_id=meeting_id, object_type="participant",
          object_id=p.id, after={k: str(v) for k, v in changes.items()})
    return participant_out(p)


@router.post("/{meeting_id}/participants/{participant_id}/notified", response_model=ParticipantOut)
def participant_notified(meeting_id: str, participant_id: str, user: User = Depends(current_user),
                         db: Session = Depends(get_db)) -> ParticipantOut:
    meeting_for(db, user, meeting_id, "capture")
    p = db.get(Participant, participant_id)
    if p is None or p.meeting_id != meeting_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Участник не найден")
    p.notified_at = utcnow()
    audit(db, action="participant.notified", actor_user_id=user.id, meeting_id=meeting_id,
          object_type="participant", object_id=p.id)
    return participant_out(p)


class MemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["manager", "viewer", "secretary"]


class MemberOut(BaseModel):
    user_id: str
    email: str
    display_name: str
    role: str


@router.get("/{meeting_id}/members", response_model=list[MemberOut])
def list_members(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[MemberOut]:
    meeting_for(db, user, meeting_id, "read")
    rows = db.execute(select(MeetingMember, User).join(User, User.id == MeetingMember.user_id)
                      .where(MeetingMember.meeting_id == meeting_id)).all()
    return [MemberOut(user_id=u.id, email=u.email, display_name=u.display_name, role=m.role) for m, u in rows]


@router.post("/{meeting_id}/members", response_model=MemberOut, status_code=201)
def add_member(meeting_id: str, body: MemberIn, user: User = Depends(current_user),
               db: Session = Depends(get_db)) -> MemberOut:
    meeting_for(db, user, meeting_id, "edit")
    target = db.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден")
    existing = db.scalar(select(MeetingMember).where(MeetingMember.meeting_id == meeting_id,
                                                     MeetingMember.user_id == target.id))
    if existing is None:
        existing = MeetingMember(meeting_id=meeting_id, user_id=target.id, role=body.role)
        db.add(existing)
    else:
        existing.role = body.role
    audit(db, action="member.set", actor_user_id=user.id, meeting_id=meeting_id, object_type="user",
          object_id=target.id, after={"role": body.role})
    return MemberOut(user_id=target.id, email=target.email, display_name=target.display_name, role=body.role)


@router.get("/{meeting_id}/capture-sessions/active")
def active_capture(meeting_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    meeting_for(db, user, meeting_id, "read")
    cs = db.scalar(select(CaptureSession).where(CaptureSession.meeting_id == meeting_id)
                   .order_by(CaptureSession.created_at.desc()).limit(1))
    return {"capture_session_id": cs.id if cs else None, "state": cs.state if cs else None}
