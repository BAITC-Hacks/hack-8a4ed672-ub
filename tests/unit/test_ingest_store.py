from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from hattama.ingest.store import EpochWriter, SequenceTracker, read_index, read_pcm_range
from hattama_contracts.frame import AudioFrame


def frame(seq: int, start: int, count: int = 4, value: int = 1000) -> AudioFrame:
    return AudioFrame(0, 1, 0, seq, 16000, count, 1_000_000 + seq, start, struct.pack(f"<{count}h", *([value] * count)))


def test_in_order_advances_watermark() -> None:
    t = SequenceTracker()
    for i in range(5):
        assert t.accept(i, i * 4, 4, now=0.0) == []
    assert (t.contiguous_seq, t.contiguous_end) == (4, 20)


def test_duplicate_is_idempotent() -> None:
    t = SequenceTracker()
    t.accept(0, 0, 4, 0.0)
    t.accept(1, 4, 4, 0.0)
    assert t.is_duplicate(1)
    t.accept(1, 4, 4, 0.0)
    assert t.duplicates == 1
    assert t.contiguous_seq == 1


def test_reordered_frame_fills_hole_without_gap() -> None:
    t = SequenceTracker(reorder_timeout_s=2.0)
    t.accept(0, 0, 4, 0.0)
    assert t.accept(2, 8, 4, 0.1) == []
    assert t.contiguous_seq == 0 and t.pending_count == 1
    assert t.accept(1, 4, 4, 0.2) == []
    assert (t.contiguous_seq, t.contiguous_end) == (2, 12)


def test_missing_frame_becomes_gap_after_timeout() -> None:
    t = SequenceTracker(reorder_timeout_s=2.0)
    t.accept(0, 0, 4, 0.0)
    t.accept(2, 8, 4, 0.5)
    gaps = t.flush_timeouts(3.0)
    assert len(gaps) == 1
    g = gaps[0]
    assert (g.from_sequence, g.to_sequence, g.start_sample, g.end_sample, g.reason) == (1, 1, 4, 8, "sequence_gap")
    assert t.contiguous_seq == 2
    assert t.in_declared_gap(1) is not None


def test_reorder_window_overflow_declares_gap_immediately() -> None:
    t = SequenceTracker(reorder_window=3, reorder_timeout_s=100)
    t.accept(0, 0, 4, 0.0)
    gaps = []
    for seq in range(2, 7):
        gaps += t.accept(seq, seq * 4, 4, 0.0)
    assert gaps and gaps[0].from_sequence == 1
    assert t.contiguous_seq == 6


def test_client_reported_loss_advances_and_records_gap() -> None:
    t = SequenceTracker()
    t.accept(0, 0, 4, 0.0)
    gaps = t.report_lost(1, 10, 4, 40, 0.1)
    assert gaps[0].reason == "client_buffer_overflow" and gaps[0].end_sample == 44
    assert t.contiguous_seq == 10
    t.accept(11, 44, 4, 0.2)
    assert t.contiguous_seq == 11


def test_writer_is_position_addressed_and_resend_is_idempotent(tmp_path: Path) -> None:
    w = EpochWriter(tmp_path / "e.pcm", 1)
    w.write(frame(0, 0, value=100))
    w.write(frame(2, 8, value=300))  # hole at 4..8 reads as silence
    w.write(frame(0, 0, value=100))  # duplicate resend
    w.sync()
    w.close()
    audio = read_pcm_range(tmp_path / "e.pcm", 1, 0, 12)
    assert np.allclose(audio[:4], 100 / 32768)
    assert np.allclose(audio[4:8], 0.0)
    assert np.allclose(audio[8:12], 300 / 32768)
    assert [r[0] for r in read_index(tmp_path / "e.idx")] == [0, 2, 0]
    # reopening appends/overwrites in place without truncating
    w2 = EpochWriter(tmp_path / "e.pcm", 1)
    w2.write(frame(1, 4, value=200))
    w2.close()
    audio = read_pcm_range(tmp_path / "e.pcm", 1, 0, 12)
    assert np.allclose(audio[4:8], 200 / 32768)


def test_read_beyond_written_is_zero_padded(tmp_path: Path) -> None:
    w = EpochWriter(tmp_path / "e.pcm", 2)
    w.write(AudioFrame(0, 2, 0, 0, 16000, 2, 0, 0, struct.pack("<4h", 1000, 3000, 1000, 3000)))
    w.close()
    audio = read_pcm_range(tmp_path / "e.pcm", 2, 0, 5)
    assert len(audio) == 5
    assert np.allclose(audio[:2], 2000 / 32768)  # stereo averaged to mono
    assert np.allclose(audio[2:], 0.0)
