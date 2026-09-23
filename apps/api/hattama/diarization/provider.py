"""Local diarization (pyannote/speaker-diarization-community-1). Status: NOT_VERIFIED in this repository —
the gated weights were not available on the build machine (conditions not accepted). The code path loads
ONLY a prepared local directory with telemetry disabled; there is no pyannoteAI cloud path."""

from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import select

from hattama.audio.reader import EpochReader
from hattama.config import Settings
from hattama.db.models import CaptureSession, CaptureSource, SourceEpoch, Speaker, TranscriptSegment
from hattama.db.session import session_scope
from hattama.domain.enums import SourceKind
from hattama.modelstore.manifest import ModelNotPreparedError, load_manifest, resolve_local_model


class DiarizationUnavailableError(RuntimeError):
    pass


def _pipeline(settings: Settings):  # type: ignore[no-untyped-def]
    try:
        entry = load_manifest(settings.model_manifest)["pyannote-community-1"]
        path = resolve_local_model(entry, settings.models_dir)
    except ModelNotPreparedError as exc:
        raise DiarizationUnavailableError("веса pyannote community-1 не подготовлены (gated)") from exc
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise DiarizationUnavailableError("pyannote.audio не установлен (python tasks.py setup --diarization)") from exc
    return Pipeline.from_pretrained(str(path))


def diarize_revision(settings: Settings, meeting_id: str, revision_id: str) -> dict[str, Any]:
    """Diarize meeting-audio sources and split speaker labels onto final segments (max-overlap per word;
    words inside overlapping turns are flagged `overlap`)."""
    pipeline = _pipeline(settings)
    import torch

    turns_total = 0
    with session_scope() as s:
        rows = s.execute(select(SourceEpoch, CaptureSource).join(CaptureSource, CaptureSource.id == SourceEpoch.source_id)
                         .join(CaptureSession, CaptureSession.id == CaptureSource.capture_session_id)
                         .where(CaptureSession.meeting_id == meeting_id,
                                CaptureSource.kind.in_([SourceKind.TAB_AUDIO, SourceKind.BOT_AUDIO,
                                                        SourceKind.LOCAL_MICROPHONE, SourceKind.UPLOAD]))).all()
        plans = [(e.rel_path, e.sample_rate, e.channel_count, e.durable_samples, src.source_key) for e, src in rows]
    for rel, rate, ch, n, source_key in plans:
        audio = EpochReader(settings.data_dir / rel, rate, ch).read_available(n, final=True)
        output = pipeline({"waveform": torch.from_numpy(audio[np.newaxis, :]), "sample_rate": 16000})
        annotation = getattr(output, "speaker_diarization", output)
        turns = [(t.start, t.end, label) for t, _, label in annotation.itertracks(yield_label=True)]
        turns_total += len(turns)
        with session_scope() as s:
            speakers: dict[str, Speaker] = {}
            for label in sorted({t[2] for t in turns}):
                sp = Speaker(meeting_id=meeting_id, revision_id=revision_id, label=label, source_key=source_key)
                s.add(sp)
                speakers[label] = sp
            s.flush()
            for seg in s.scalars(select(TranscriptSegment).where(TranscriptSegment.revision_id == revision_id,
                                                                 TranscriptSegment.source_key == source_key)):
                a, b = seg.start_ms / 1000, seg.end_ms / 1000
                overlap: dict[str, float] = {}
                for ts, te, label in turns:
                    ov = min(b, te) - max(a, ts)
                    if ov > 0:
                        overlap[label] = overlap.get(label, 0) + ov
                if overlap:
                    best = max(overlap, key=overlap.get)  # type: ignore[arg-type]
                    seg.speaker_id = speakers[best].id
                    seg.overlap = len([v for v in overlap.values() if v > 0.3]) > 1
    return {"mode": "pyannote", "turns": turns_total}
