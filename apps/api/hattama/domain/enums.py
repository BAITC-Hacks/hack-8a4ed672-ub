from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    ADMIN = "admin"
    SECRETARY = "secretary"
    MANAGER = "manager"
    ASSIGNEE = "assignee"


class MemberRole(StrEnum):
    SECRETARY = "secretary"
    MANAGER = "manager"
    VIEWER = "viewer"


class MeetingStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"  # consent recorded, capture may start
    LIVE = "LIVE"
    PROCESSING = "PROCESSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    APPROVED = "APPROVED"
    FAILED = "FAILED"


class Platform(StrEnum):
    GOOGLE_MEET = "google_meet"
    TEAMS_WEB = "teams_web"
    ZOOM_WEB = "zoom_web"
    IN_PERSON = "in_person"
    OTHER = "other"


class LanguageMode(StrEnum):
    MIXED = "mixed"
    RU = "ru"
    KK = "kk"
    AUTO = "auto"


class CaptureMode(StrEnum):
    COMPANION = "companion"  # Chromium extension (tab audio + optional mic)
    BROWSER_TAB = "browser_tab"  # dashboard display capture (tab audio + optional mic)
    LOCAL_MIC = "local_mic"  # microphone of this computer via the dashboard page
    BOT = "bot"  # self-hosted meeting-agent
    UPLOAD = "upload"  # audio/video file (additional input, not a replacement for live)


class CaptureState(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class SourceKind(StrEnum):
    TAB_AUDIO = "tab_audio"
    MICROPHONE = "microphone"
    LOCAL_MICROPHONE = "local_microphone"
    BOT_AUDIO = "bot_audio"
    UPLOAD = "upload"


class SegmentState(StrEnum):
    PARTIAL = "partial"
    STABLE = "stable"
    FINAL = "final"


class RevisionKind(StrEnum):
    LIVE = "live"
    FINAL = "final"
    MANUAL = "manual"


class ReviewState(StrEnum):
    PROPOSED = "proposed"  # machine suggestion, not reviewed
    NEEDS_REVIEW = "needs_review"  # machine suggestion with explicit reasons for a human
    CONFIRMED = "confirmed"  # confirmed by secretary
    EDITED = "edited"  # edited by a human (never overwritten by machine results)
    REJECTED = "rejected"


class ExecutionState(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


class AssigneeType(StrEnum):
    PARTICIPANT = "participant"  # present at the meeting
    MENTIONED_PERSON = "mentioned_person"  # named but not (known to be) present
    DEPARTMENT = "department"
    GROUP = "group"
    UNKNOWN = "unknown"


class IssueSeverity(StrEnum):
    BLOCKER = "blocker"
    WARNING = "warning"
    INFO = "info"


class IssueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BotState(StrEnum):
    CREATED = "CREATED"
    JOINING = "JOINING"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    WAITING_FOR_ADMISSION = "WAITING_FOR_ADMISSION"
    AWAITING_RECORDING_APPROVAL = "AWAITING_RECORDING_APPROVAL"
    RECORDING = "RECORDING"
    RECONNECTING = "RECONNECTING"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    ENDED = "ENDED"
    FAILED = "FAILED"


class NotificationKind(StrEnum):
    ASSIGNED = "assigned"
    DUE_SOON = "due_soon"
    DUE_TODAY = "due_today"
    OVERDUE = "overdue"
    CHANGED = "changed"


class NotificationStatus(StrEnum):
    SCHEDULED = "scheduled"
    DELIVERED = "delivered"
    READ = "read"
    CANCELLED = "cancelled"
