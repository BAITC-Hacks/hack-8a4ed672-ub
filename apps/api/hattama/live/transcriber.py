"""Rolling-window live transcription for ONE audio stream (one source epoch).

Algorithm (LocalAgreement-2 with VAD-assisted phrase ends):
* audio accumulates in a buffer starting at `buffer_start` (seconds from epoch start, 16 kHz domain);
* every `step_s` of new audio the whole buffer is transcribed with word timestamps;
* words already committed (end <= committed_end) are dropped; the longest common prefix of the previous
  and the current hypothesis is committed ("stable" = confirmed by two overlapping windows);
* when VAD sees >= `phrase_end_silence_s` of silence after speech, the rest of the current hypothesis
  up to the speech end is committed as well (phrase finished, no need to wait for another window);
* committed words are grouped into utterances; an utterance becomes a STABLE segment at a phrase end,
  a long pause, or when it exceeds `max_utterance_s` (split at the largest pause);
* the buffer is trimmed to the committed point once it exceeds `max_window_s`; if nothing could be
  committed for that long, the hypothesis is force-committed and marked for review.
Timestamps always come from the ASR word times plus the buffer offset, i.e. the original timeline.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field

import numpy as np

from hattama.asr.base import ASRProvider, DecodeOptions, Word
from hattama.audio.vad import Vad

SR = 16000
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)
SENTENCE_END = (".", "?", "!", "…")
# Well-known Whisper hallucinations on silence/noise (subtitle credits etc.). Segments containing them are
# FLAGGED for review, never silently deleted: the phrase might have really been said.
HALLUCINATION_MARKERS = ("продолжение следует", "субтитры", "спасибо за просмотр", "редактор субтитров",
                         "подписывайтесь на канал", "dimatorzok", "amara.org", "we'll be right back",
                         "thank you for watching", "thanks for watching", "please subscribe")
SPEECH_PAD_S = 0.2


def is_suspicious_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in HALLUCINATION_MARKERS)


def norm_word(text: str) -> str:
    return _PUNCT.sub("", text.lower().replace("ё", "е"))


@dataclass(frozen=True)
class LiveConfig:
    step_s: float = 1.5
    max_window_s: float = 20.0
    min_first_window_s: float = 1.0
    phrase_end_silence_s: float = 0.8
    pause_split_s: float = 1.2
    max_utterance_s: float = 25.0
    language_mode: str = "mixed"
    beam_size: int = 1
    context_chars: int = 120


@dataclass
class TimedWord:
    text: str
    start: float  # seconds from epoch start
    end: float
    probability: float


@dataclass
class LiveOutput:
    kind: str  # "partial" | "stable"
    utterance_id: str
    revision: int
    text: str
    start_s: float
    end_s: float
    words: list[TimedWord]
    language: str | None
    review_reasons: list[str] = field(default_factory=list)
    avg_probability: float | None = None


@dataclass
class StepStats:
    audio_s: float
    processing_s: float
    committed_words: int
    partial_words: int


class LiveTranscriber:
    def __init__(self, asr: ASRProvider, vad: Vad, config: LiveConfig, glossary: str | None = None,
                 start_s: float = 0.0) -> None:
        self.asr = asr
        self.vad = vad
        self.cfg = config
        self.glossary = glossary
        self.buffer = np.zeros(0, dtype=np.float32)
        self.buffer_start = start_s
        self.committed_end = start_s
        self.prev_hyp: list[TimedWord] = []
        self.utterance: list[TimedWord] = []
        self.utterance_id = uuid.uuid4().hex
        self.utterance_rev = 0
        self.utterance_reasons: set[str] = set()
        self.committed_text_tail = ""
        self.new_audio_s = 0.0
        self.language: str | None = None
        self.last_stats: StepStats | None = None

    # ------------------------------------------------------------------ public API
    @property
    def buffer_end(self) -> float:
        return self.buffer_start + len(self.buffer) / SR

    def push(self, audio_16k: np.ndarray) -> None:
        if len(audio_16k):
            self.buffer = np.concatenate([self.buffer, audio_16k.astype(np.float32)])
            self.new_audio_s += len(audio_16k) / SR

    def ready(self) -> bool:
        return self.new_audio_s >= self.cfg.step_s and len(self.buffer) / SR >= self.cfg.min_first_window_s

    def step(self, final: bool = False) -> list[LiveOutput]:
        """Run one recognition pass. With final=True everything left is committed (end of stream)."""
        self.new_audio_s = 0.0
        outputs: list[LiveOutput] = []
        if len(self.buffer) < SR // 5:
            if final:
                outputs += self._close_utterance(reason=None)
            return outputs
        speech = self.vad.speech_intervals(self.buffer)
        uncommitted_from = max(0, int((self.committed_end - self.buffer_start) * SR))
        has_speech_after = any(e > uncommitted_from for _, e in speech)
        if not has_speech_after and not self.prev_hyp:
            # silence: nothing new to recognise; close a pending utterance and drop old audio
            if self.utterance and (self.buffer_end - self.utterance[-1].end) >= self.cfg.phrase_end_silence_s:
                outputs += self._close_utterance(reason=None)
            self._trim_to(max(self.committed_end, self.buffer_end - 0.5))
            if final:
                outputs += self._close_utterance(reason=None)
            return outputs

        started = time.perf_counter()
        result = self.asr.transcribe(self.buffer, DecodeOptions(
            language_mode=self.cfg.language_mode,  # type: ignore[arg-type]
            beam_size=self.cfg.beam_size, word_timestamps=True, initial_prompt=self._prompt()))
        processing = time.perf_counter() - started
        words = [TimedWord(w.text.strip(), self.buffer_start + w.start, self.buffer_start + w.end, w.probability)
                 for seg in result.segments for w in seg.words if w.text.strip()]
        if result.language:
            self.language = result.language
        # words lying entirely outside VAD speech are hallucinations on silence -> drop them
        words = [w for w in words if self._in_speech(w, speech)]
        # drop words that are already committed (overlap of consecutive windows)
        hyp = [w for w in words if w.end > self.committed_end + 0.02]

        committed_now = self._agree(self.prev_hyp, hyp)
        rest = hyp[len(committed_now):]
        speech_end = self._speech_end(speech)
        phrase_finished = speech_end is not None and (self.buffer_end - speech_end) >= self.cfg.phrase_end_silence_s
        if final or phrase_finished:
            limit = self.buffer_end if final else (speech_end or self.buffer_end) + 0.3
            tail = [w for w in rest if w.end <= limit]
            committed_now += tail
            rest = rest[len(tail):]
        forced = False
        if not committed_now and (self.buffer_end - self.buffer_start) > self.cfg.max_window_s and rest:
            committed_now, rest, forced = rest, [], True
        self.prev_hyp = rest
        for w in committed_now:
            self._commit_word(w)
        if forced:
            self.utterance_reasons.add("forced_commit")

        outputs += self._split_utterance_if_needed()
        if self.utterance and (final or phrase_finished) and not self.prev_hyp:
            outputs += self._close_utterance(reason=None)
        if self.utterance or self.prev_hyp:
            outputs.append(self._partial())
        if final:
            if self.prev_hyp:
                for w in self.prev_hyp:
                    self._commit_word(w)
                self.prev_hyp = []
            outputs += self._close_utterance(reason=None)
        if self.buffer_end - self.buffer_start > self.cfg.max_window_s:
            self._trim_to(self.committed_end)
        self.last_stats = StepStats(len(self.buffer) / SR, processing, len(committed_now), len(self.prev_hyp))
        return outputs

    def finish(self) -> list[LiveOutput]:
        return self.step(final=True)

    def pending_seconds(self) -> float:
        return max(0.0, self.buffer_end - self.committed_end)

    # ------------------------------------------------------------------ internals
    def _prompt(self) -> str | None:
        parts = []
        if self.glossary:
            parts.append(self.glossary)
        if self.committed_text_tail:
            parts.append(self.committed_text_tail[-self.cfg.context_chars:])
        return " ".join(parts) or None

    @staticmethod
    def _agree(prev: list[TimedWord], cur: list[TimedWord]) -> list[TimedWord]:
        agreed: list[TimedWord] = []
        for a, b in zip(prev, cur, strict=False):
            if norm_word(a.text) != norm_word(b.text) or abs(a.start - b.start) > 1.0:
                break
            agreed.append(b)
        return agreed

    def _in_speech(self, w: TimedWord, speech: list[tuple[int, int]]) -> bool:
        for s, e in speech:
            if w.end > self.buffer_start + s / SR - SPEECH_PAD_S and w.start < self.buffer_start + e / SR + SPEECH_PAD_S:
                return True
        return False

    def _speech_end(self, speech: list[tuple[int, int]]) -> float | None:
        if not speech:
            return None
        return self.buffer_start + speech[-1][1] / SR

    def _commit_word(self, w: TimedWord) -> None:
        self.utterance.append(w)
        self.committed_end = max(self.committed_end, w.end)
        self.committed_text_tail = (self.committed_text_tail + " " + w.text).strip()[-400:]

    def _split_utterance_if_needed(self) -> list[LiveOutput]:
        outputs: list[LiveOutput] = []
        while self.utterance:
            # split at long pauses
            split_at = None
            for i in range(1, len(self.utterance)):
                if self.utterance[i].start - self.utterance[i - 1].end >= self.cfg.pause_split_s:
                    split_at = i
                    break
                if self.utterance[i - 1].text.endswith(SENTENCE_END) and \
                        self.utterance[i].start - self.utterance[i - 1].end >= 0.6:
                    split_at = i
                    break
            duration = self.utterance[-1].end - self.utterance[0].start
            if split_at is None and duration > self.cfg.max_utterance_s:
                gaps = [(self.utterance[i].start - self.utterance[i - 1].end, i) for i in range(1, len(self.utterance))]
                split_at = max(gaps)[1] if gaps else None
            if split_at is None:
                break
            head, self.utterance = self.utterance[:split_at], self.utterance[split_at:]
            outputs.append(self._stable_from(head))
            self.utterance_id = uuid.uuid4().hex
            self.utterance_rev = 0
        return outputs

    def _stable_from(self, words: list[TimedWord]) -> LiveOutput:
        reasons = sorted(self.utterance_reasons)
        self.utterance_reasons = set()
        probs = [w.probability for w in words]
        avg = float(np.mean(probs)) if probs else None
        if avg is not None and avg < 0.55:
            reasons.append("low_asr_confidence")
        if is_suspicious_text(_join(words)):
            reasons.append("possible_hallucination")
        return LiveOutput("stable", self.utterance_id, self.utterance_rev + 1, _join(words), words[0].start,
                          words[-1].end, list(words), self.language, reasons, avg)

    def _close_utterance(self, reason: str | None) -> list[LiveOutput]:
        if not self.utterance:
            return []
        if reason:
            self.utterance_reasons.add(reason)
        out = self._stable_from(self.utterance)
        self.utterance = []
        self.utterance_id = uuid.uuid4().hex
        self.utterance_rev = 0
        return [out]

    def _partial(self) -> LiveOutput:
        words = self.utterance + self.prev_hyp
        self.utterance_rev += 1
        return LiveOutput("partial", self.utterance_id, self.utterance_rev, _join(words), words[0].start,
                          words[-1].end, list(words), self.language)

    def _trim_to(self, t: float) -> None:
        t = min(max(t, self.buffer_start), self.buffer_end)
        drop = int((t - self.buffer_start) * SR)
        if drop > 0:
            self.buffer = self.buffer[drop:]
            self.buffer_start += drop / SR


def _join(words: list[TimedWord]) -> str:
    text = ""
    for w in words:
        token = w.text
        if not text:
            text = token
        elif token and token[0] in ",.!?:;…)»":
            text += token
        else:
            text += " " + token
    return text.strip()
