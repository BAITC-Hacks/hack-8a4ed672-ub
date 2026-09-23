"""Final ASR pass over the whole recording (per source), run by the ASR worker between live ticks.

Memory is bounded: audio is read sequentially and cut at VAD pauses into <= MAX_CHUNK_S chunks.
The job is idempotent: a restarted job deletes its unfinished revision's segments and starts over.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from hattama.asr.base import ASRProvider, AsrUnavailableError, DecodeOptions
from hattama.audio.reader import EpochReader
from hattama.audio.vad import Vad
from hattama.config import Settings
from hattama.db.models import (
    CaptureSession,
    CaptureSource,
    Job,
    Meeting,
    Participant,
    SourceEpoch,
    TranscriptRevision,
    TranscriptSegment,
)
from hattama.db.session import session_scope
from hattama.domain.enums import CaptureState, RevisionKind, SegmentState
from hattama.jobs import queue
from hattama.live.transcriber import is_suspicious_text
from hattama.runtime import participant_glossary
from hattama.services.events import publish

log = logging.getLogger(__name__)
SR = 16000
MAX_CHUNK_S = 28.0
MIN_CUT_S = 12.0
READ_BLOCK_S = 30.0


@dataclass
class _SourcePlan:
    epoch_id: str
    rel_path: str
    sample_rate: int
    channel_count: int
    durable_samples: int
    source_key: str
    capture_session_id: str
    offset_ms: float


def find_cut(vad: Vad, audio: np.ndarray) -> tuple[int, bool]:
    """Cut index (samples) at the last pause within [MIN_CUT_S, MAX_CHUNK_S]; (index, hard_cut)."""
    limit = min(len(audio), int(MAX_CHUNK_S * SR))
    speech = vad.speech_intervals(audio[:limit])
    best = None
    for (_, end_a), (start_b, _) in zip(speech, speech[1:], strict=False):
        mid = (end_a + start_b) // 2
        if MIN_CUT_S * SR <= mid <= limit and start_b - end_a >= int(0.25 * SR):
            best = mid
    if best is not None:
        return best, False
    if speech and speech[-1][1] < limit - int(0.3 * SR):
        return speech[-1][1] + int(0.15 * SR), False
    if not speech:
        return limit, False
    return limit, True


class FinalAsrRunner:
    def __init__(self, settings: Settings, provider_factory: Callable[[], ASRProvider], vad: Vad, cfg: dict,
                 worker_id: str, yield_to_live: Callable[[], bool] | None = None) -> None:
        self.settings = settings
        self.provider_factory = provider_factory
        self._provider: ASRProvider | None = None
        self.vad = vad
        self.cfg = cfg
        self.worker_id = worker_id
        self.yield_to_live = yield_to_live

    @property
    def provider(self) -> ASRProvider:
        if self._provider is None:
            self._provider = self.provider_factory()
        return self._provider

    def tick(self) -> bool:
        with session_scope() as s:
            job = queue.claim(s, queue="asr", worker_id=self.worker_id, lease_s=300, kinds=["final_asr"])
            job_id = job.id if job else None
            payload = dict(job.payload) if job else {}
            meeting_id = job.meeting_id if job else None
        if job_id is None:
            return False
        assert meeting_id is not None
        try:
            with session_scope() as s:
                if not queue.try_acquire_lock(s, "heavy", self.worker_id, ttl_s=600):
                    queue.fail(s, job_id, self.worker_id, "heavy stage busy", retryable=True, retry_delay_s=5)
                    return True
            try:
                result = self.run_job(job_id, meeting_id, payload)
            finally:
                with session_scope() as s:
                    queue.release_lock(s, "heavy", self.worker_id)
            with session_scope() as s:
                queue.complete(s, job_id, self.worker_id, result)
                queue.enqueue(s, kind="diarize", key=f"diarize:{meeting_id}:{result['revision_id']}",
                              meeting_id=meeting_id, payload={"revision_id": result["revision_id"]}, priority=60)
                publish(s, meeting_id, "job.state", {"kind": "final_asr", "state": "succeeded", **result})
        except queue.JobCancelledError:
            with session_scope() as s:
                queue.fail(s, job_id, self.worker_id, "cancelled", retryable=False)
        except AsrUnavailableError as exc:
            with session_scope() as s:
                state = queue.fail(s, job_id, self.worker_id, str(exc), retryable=True, retry_delay_s=60)
                publish(s, meeting_id, "job.state", {"kind": "final_asr", "state": state, "error": str(exc)})
        return True

    def _plans(self, session: Session, meeting_id: str) -> list[_SourcePlan]:
        rows = session.execute(
            select(SourceEpoch, CaptureSource, CaptureSession)
            .join(CaptureSource, CaptureSource.id == SourceEpoch.source_id)
            .join(CaptureSession, CaptureSession.id == CaptureSource.capture_session_id)
            .where(CaptureSession.meeting_id == meeting_id, CaptureSession.state == CaptureState.STOPPED)
            .order_by(CaptureSession.created_at, CaptureSource.source_index, SourceEpoch.epoch)
        ).all()
        plans = []
        for epoch, src, cs in rows:
            origin = cs.timeline_origin_us if cs.timeline_origin_us is not None else epoch.epoch_start_wall_us
            plans.append(_SourcePlan(epoch.id, epoch.rel_path, epoch.sample_rate, epoch.channel_count,
                                     epoch.durable_samples, src.source_key, cs.id,
                                     (epoch.epoch_start_wall_us - origin) / 1000.0))
        return plans

    def run_job(self, job_id: str, meeting_id: str, payload: dict) -> dict:
        with session_scope() as s:
            meeting = s.get(Meeting, meeting_id)
            assert meeting is not None
            language_mode = meeting.language_mode
            names = [p.display_name for p in s.scalars(select(Participant).where(Participant.meeting_id == meeting_id))]
            rev = s.scalar(select(TranscriptRevision).where(TranscriptRevision.job_id == job_id))
            if rev is not None and rev.status == "complete":
                return {"revision_id": rev.id, "segments": 0, "reused": True}
            if rev is None:
                number = (s.scalar(select(func.max(TranscriptRevision.number))
                                   .where(TranscriptRevision.meeting_id == meeting_id)) or 0) + 1
                rev = TranscriptRevision(meeting_id=meeting_id, number=number, kind=RevisionKind.FINAL,
                                         status="building", job_id=job_id)
                s.add(rev)
                s.flush()
            else:
                s.execute(delete(TranscriptSegment).where(TranscriptSegment.revision_id == rev.id))
            rev_id = rev.id
            plans = self._plans(s, meeting_id)
        provider = self.provider
        options = DecodeOptions(language_mode=language_mode,  # type: ignore[arg-type]
                                beam_size=int(self.cfg.get("beam_size", 5)), word_timestamps=True,
                                initial_prompt=participant_glossary(names), vad_filter=True)
        total_s = sum(p.durable_samples / p.sample_rate for p in plans) or 1.0
        done_s = 0.0
        seq = 0
        processing = 0.0
        started = time.perf_counter()
        for plan in plans:
            reader = EpochReader(self.settings.data_dir / plan.rel_path, plan.sample_rate, plan.channel_count)
            pending = np.zeros(0, dtype=np.float32)
            pending_start = 0.0  # seconds from epoch start
            while True:
                block = reader.read_available(plan.durable_samples, max_native=int(READ_BLOCK_S * plan.sample_rate),
                                              final=reader.read_pos >= plan.durable_samples)
                eof = reader.finished or (len(block) == 0 and reader.read_pos >= plan.durable_samples)
                pending = np.concatenate([pending, block])
                while len(pending) >= int(MAX_CHUNK_S * SR) or (eof and len(pending) > 0):
                    cut, hard = find_cut(self.vad, pending) if len(pending) >= int(MAX_CHUNK_S * SR) \
                        else (len(pending), False)
                    chunk = pending[:cut]
                    t0 = time.perf_counter()
                    result = provider.transcribe(chunk, options)
                    processing += time.perf_counter() - t0
                    with session_scope() as s:
                        for seg in result.segments:
                            if not seg.text:
                                continue
                            reasons = []
                            if seg.avg_logprob is not None and seg.avg_logprob < -1.0:
                                reasons.append("low_asr_confidence")
                            if hard and seg.end >= (cut / SR) - 1.0:
                                reasons.append("chunk_boundary")
                            if is_suspicious_text(seg.text):
                                reasons.append("possible_hallucination")
                            base = plan.offset_ms / 1000 + pending_start
                            s.add(TranscriptSegment(
                                meeting_id=meeting_id, revision_id=rev_id, capture_session_id=plan.capture_session_id,
                                source_key=plan.source_key, seq=seq, start_ms=int(round((base + seg.start) * 1000)),
                                end_ms=int(round((base + seg.end) * 1000)), text=seg.text, original_text=seg.text,
                                words=[{"w": w.text.strip(), "s": round(base + w.start, 3), "e": round(base + w.end, 3),
                                        "p": round(w.probability, 3)} for w in seg.words],
                                language=seg.language, state=SegmentState.FINAL, review_reasons=reasons,
                                avg_logprob=seg.avg_logprob))
                            seq += 1
                        done_s += len(chunk) / SR
                        queue.set_progress(s, job_id, {"processed_s": round(done_s, 1), "total_s": round(total_s, 1)})
                        if not queue.heartbeat(s, job_id, self.worker_id, lease_s=300):
                            raise queue.JobCancelledError(job_id)
                    pending = pending[cut:]
                    pending_start += cut / SR
                    if self.yield_to_live is not None:
                        self.yield_to_live()  # keep live meetings flowing while the final pass runs
                if eof:
                    break
        with session_scope() as s:
            rev = s.get(TranscriptRevision, rev_id)
            assert rev is not None
            rev.status = "complete"
            rev.asr_model = provider.model_id
            rev.asr_params = {"beam_size": options.beam_size, "language_mode": options.language_mode,
                              "vad_filter": True, "compute_type": provider.compute_type, "device": provider.device}
        wall = time.perf_counter() - started
        return {"revision_id": rev_id, "segments": seq, "audio_s": round(done_s, 1),
                "processing_s": round(processing, 1), "rtf": round(processing / max(done_s, 1e-6), 3),
                "wall_s": round(wall, 1)}


def job_for(session: Session, job_id: str) -> Job | None:
    return session.get(Job, job_id)
