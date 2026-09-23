"""JSON control messages of `hattama.audio.v1` and dashboard live events.

JSON Schema is generated from these models into packages/contracts/schemas/ (tasks.py contracts);
TypeScript types are generated from that schema, so there is a single source of truth.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

PROTOCOL = "hattama.audio.v1"
SourceKind = Literal["tab_audio", "microphone", "local_microphone", "bot_audio"]
SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- client -> server
class ClientInfo(_Msg):
    name: str = Field(max_length=64)
    version: str = Field(max_length=32)
    platform: str = Field(default="", max_length=64)


class Hello(_Msg):
    type: Literal["hello"]
    protocol: Literal["hattama.audio.v1"]
    token: str | None = Field(default=None, max_length=256)
    capture_session_id: str | None = Field(default=None, max_length=64)
    client: ClientInfo


class SourceOpen(_Msg):
    type: Literal["source_open"]
    source_id: str = Field(pattern=SOURCE_ID_PATTERN)
    kind: SourceKind
    sample_rate: int = Field(ge=8000, le=96000)
    channel_count: int = Field(ge=1, le=2)
    capture_epoch: int = Field(ge=0, le=2**31)
    epoch_start_wall_us: int = Field(ge=0)
    label: str = Field(default="", max_length=120)


class SourceClose(_Msg):
    type: Literal["source_close"]
    source_index: int = Field(ge=0, le=65535)
    capture_epoch: int = Field(ge=0)
    final_sequence: int = Field(ge=-1, description="-1 если ни одного кадра не отправлено")
    reason: Literal["user_stop", "track_ended", "permission_revoked", "tab_closed", "stop_requested",
                    "device_changed", "error"]


class GapReport(_Msg):
    type: Literal["gap_report"]
    source_index: int = Field(ge=0, le=65535)
    capture_epoch: int = Field(ge=0)
    from_sequence: int = Field(ge=0)
    to_sequence: int = Field(ge=0, description="включительно")
    start_sample: int = Field(ge=0)
    lost_samples: int = Field(ge=0)
    reason: Literal["client_buffer_overflow", "capture_glitch", "encoder_error"]


class SourceState(_Msg):
    type: Literal["source_state"]
    source_index: int = Field(ge=0, le=65535)
    state: Literal["muted", "unmuted", "silence", "signal", "track_ended", "permission_denied",
                   "device_missing"]
    at_us: int = Field(ge=0)


class StopSession(_Msg):
    type: Literal["stop_session"]
    reason: Literal["user_stop", "tab_closed", "error"] = "user_stop"


class Ping(_Msg):
    type: Literal["ping"]
    t: int


ClientMessage = Annotated[
    Union[Hello, SourceOpen, SourceClose, GapReport, SourceState, StopSession, Ping],
    Field(discriminator="type"),
]
client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


# ---------------------------------------------------------------- server -> client
class Limits(_Msg):
    max_frame_bytes: int
    max_sources: int
    ack_level: Literal["fsync"]
    reorder_window_frames: int
    stop_grace_s: float


class Welcome(_Msg):
    type: Literal["welcome"] = "welcome"
    protocol: Literal["hattama.audio.v1"] = PROTOCOL
    capture_session_id: str
    meeting_id: str
    server_time_us: int
    limits: Limits


class SourceReady(_Msg):
    type: Literal["source_ready"] = "source_ready"
    source_id: str
    source_index: int
    capture_epoch: int
    resume_from_sequence: int
    durable_sequence: int


class Ack(_Msg):
    type: Literal["ack"] = "ack"
    source_index: int
    capture_epoch: int
    durable_sequence: int
    durable_sample: int


class GapRecorded(_Msg):
    type: Literal["gap_recorded"] = "gap_recorded"
    source_index: int
    capture_epoch: int
    start_sample: int
    end_sample: int
    reason: str


class SourceClosed(_Msg):
    type: Literal["source_closed"] = "source_closed"
    source_index: int
    capture_epoch: int
    durable_sequence: int


class StopRequested(_Msg):
    type: Literal["stop_requested"] = "stop_requested"
    reason: str


class SessionStopped(_Msg):
    type: Literal["session_stopped"] = "session_stopped"
    capture_session_id: str


class ErrorMsg(_Msg):
    type: Literal["error"] = "error"
    code: str
    message: str
    fatal: bool = False


class Pong(_Msg):
    type: Literal["pong"] = "pong"
    t: int
    server_time_us: int


ServerMessage = Annotated[
    Union[Welcome, SourceReady, Ack, GapRecorded, SourceClosed, StopRequested, SessionStopped, ErrorMsg, Pong],
    Field(discriminator="type"),
]
server_message_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)


# ---------------------------------------------------------------- dashboard live events
class LiveEvent(_Msg):
    """Envelope for /meetings/{id}/events/ws. `payload` shape depends on `type`."""

    id: int
    type: Literal[
        "capture.state", "source.state", "source.level", "source.gap", "segment.partial", "segment.stable",
        "segment.final", "asr.metrics", "action.preliminary", "meeting.status", "issue.created", "job.state",
    ]
    meeting_id: str
    at: str
    payload: dict
