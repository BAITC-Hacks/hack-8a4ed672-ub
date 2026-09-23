"""LiveTranscriber logic with a TEST-ONLY scripted ASR double (never used outside tests)."""

from __future__ import annotations

import numpy as np

from hattama.asr.base import AsrResult, AsrSegment, DecodeOptions, Word
from hattama.audio.vad import EnergyVad
from hattama.live.transcriber import LiveConfig, LiveTranscriber, norm_word

SR = 16000
# (word, start, end) on the absolute timeline; pause 1.5 s after "поставщика."
SCRIPT = [
    ("Ботагоз,", 0.2, 0.7), ("найдите", 0.8, 1.3), ("альтернативного", 1.4, 2.2), ("поставщика.", 2.3, 3.0),
    ("Ерлан,", 4.5, 4.9), ("подготовьте", 5.0, 5.6), ("претензию", 5.7, 6.3), ("до", 6.4, 6.5),
    ("пятницы.", 6.6, 7.2),
]


def make_audio(total_s: float) -> np.ndarray:
    audio = np.zeros(int(total_s * SR), dtype=np.float32)
    rng = np.random.default_rng(1)
    for _, s, e in SCRIPT:
        audio[int(s * SR):int(e * SR)] = rng.uniform(-0.3, 0.3, int(e * SR) - int(s * SR))
    return audio


class ScriptedAsr:
    """Returns the scripted words fully contained in the window; the word being spoken at the right
    edge is returned misspelled (unstable), like a real streaming hypothesis."""

    model_id = "scripted-test-double"
    device = "cpu"
    compute_type = "none"

    def __init__(self) -> None:
        self.offset = 0.0
        self.calls = 0

    def transcribe(self, audio: np.ndarray, options: DecodeOptions) -> AsrResult:
        self.calls += 1
        dur = len(audio) / SR
        words = []
        for text, s, e in SCRIPT:
            rs, re_ = s - self.offset, e - self.offset
            if re_ <= 0 or rs >= dur:
                continue
            if re_ > dur:  # cut-off word at the window edge
                words.append(Word(text[:3] + "…", max(rs, 0), dur, 0.3))
                break
            words.append(Word(text, max(rs, 0.0), re_, 0.9))
        seg = AsrSegment(" ".join(w.text for w in words), 0, dur, words, "ru")
        return AsrResult([seg], "ru", 0.99, dur, 0.01, self.model_id)

    def close(self) -> None:
        pass


def run_stream(chunk_s: float = 0.5, total_s: float = 9.5) -> tuple[list, LiveTranscriber]:
    asr = ScriptedAsr()
    tr = LiveTranscriber(asr, EnergyVad(), LiveConfig(step_s=1.0, max_window_s=12.0, phrase_end_silence_s=0.8))
    audio = make_audio(total_s)
    outputs = []
    step = int(chunk_s * SR)
    for i in range(0, len(audio), step):
        tr.push(audio[i:i + step])
        asr.offset = tr.buffer_start
        if tr.ready():
            outputs += tr.step()
    asr.offset = tr.buffer_start
    outputs += tr.finish()
    return outputs, tr


def test_every_word_committed_once_in_order_without_seam_duplicates() -> None:
    outputs, _ = run_stream()
    stable = [o for o in outputs if o.kind == "stable"]
    words = [norm_word(w.text) for o in stable for w in o.words]
    assert words == [norm_word(w) for w, _, _ in SCRIPT]


def test_utterances_split_at_pause_with_absolute_timestamps() -> None:
    outputs, _ = run_stream()
    stable = [o for o in outputs if o.kind == "stable"]
    assert len(stable) == 2
    assert stable[0].text.startswith("Ботагоз") and stable[0].text.endswith("поставщика.")
    assert abs(stable[0].start_s - 0.2) < 1e-6 and abs(stable[0].end_s - 3.0) < 1e-6
    assert abs(stable[1].start_s - 4.5) < 1e-6 and stable[1].text.endswith("пятницы.")


def test_partials_precede_stable_with_same_utterance_id_and_growing_revision() -> None:
    outputs, _ = run_stream()
    first_stable = next(o for o in outputs if o.kind == "stable")
    partials = [o for o in outputs if o.kind == "partial" and o.utterance_id == first_stable.utterance_id]
    assert partials, "a draft must be shown before the phrase is stable"
    assert [p.revision for p in partials] == sorted(p.revision for p in partials)
    assert outputs.index(partials[0]) < outputs.index(first_stable)


def test_unstable_edge_word_never_committed() -> None:
    outputs, _ = run_stream()
    assert all("…" not in w.text for o in outputs if o.kind == "stable" for w in o.words)


def test_chunk_size_does_not_change_result() -> None:
    a, _ = run_stream(chunk_s=0.25)
    b, _ = run_stream(chunk_s=0.8)
    ta = [o.text for o in a if o.kind == "stable"]
    tb = [o.text for o in b if o.kind == "stable"]
    assert ta == tb


def test_silence_does_not_call_asr() -> None:
    asr = ScriptedAsr()
    tr = LiveTranscriber(asr, EnergyVad(), LiveConfig(step_s=1.0))
    for _ in range(10):
        tr.push(np.zeros(SR, dtype=np.float32))
        if tr.ready():
            assert tr.step() == []
    assert asr.calls == 0
    assert tr.buffer_end - tr.buffer_start <= 1.0  # silence is trimmed, memory stays bounded
