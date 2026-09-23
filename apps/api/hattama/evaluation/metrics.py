"""Evaluation metrics: WER/CER with a declared normalisation, simple DER, precision/recall/F1."""

from __future__ import annotations

import re
from dataclasses import dataclass

_PUNCT = re.compile(r"[^\w\s%]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Declared normalisation: lower-case, ё->е, punctuation removed (except %), whitespace collapsed.
    Numbers are NOT converted between digits and words; such mismatches count as errors and are
    reported separately by number_accuracy()."""
    text = text.lower().replace("ё", "е")
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def _edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1]


def wer(reference: str, hypothesis: str) -> float:
    ref = normalize_text(reference).split()
    hyp = normalize_text(hypothesis).split()
    return _edit_distance(ref, hyp) / max(len(ref), 1)


def cer(reference: str, hypothesis: str) -> float:
    ref = list(normalize_text(reference).replace(" ", ""))
    hyp = list(normalize_text(hypothesis).replace(" ", ""))
    return _edit_distance(ref, hyp) / max(len(ref), 1)


def term_recall(terms: list[str], hypothesis: str) -> tuple[int, int, list[str]]:
    """How many expected terms (names, key words; matched by prefix stem) appear in the hypothesis."""
    hyp = normalize_text(hypothesis)
    missing = [t for t in terms if normalize_text(t) not in hyp]
    return len(terms) - len(missing), len(terms), missing


@dataclass
class Prf:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


def der(reference: list[tuple[float, float, str]], hypothesis: list[tuple[float, float, str]],
        collar: float = 0.25, step: float = 0.01) -> dict[str, float]:
    """Frame-based diarization error rate with optimal 1:1 speaker mapping (greedy on overlap).

    Policy: `collar` seconds around each reference boundary are excluded; overlapping reference
    speech is scored (a frame with 2 reference speakers counts 2 units)."""
    if not reference:
        return {"der": 0.0, "miss": 0.0, "false_alarm": 0.0, "confusion": 0.0}
    end = max(e for _, e, _ in reference + hypothesis)
    n = int(end / step) + 1
    excluded = [False] * n
    for s, e, _ in reference:
        for b in (s, e):
            for i in range(max(0, int((b - collar) / step)), min(n, int((b + collar) / step) + 1)):
                excluded[i] = True

    def frames(segs: list[tuple[float, float, str]]) -> list[set[str]]:
        out: list[set[str]] = [set() for _ in range(n)]
        for s, e, spk in segs:
            for i in range(int(s / step), min(n, int(e / step))):
                out[i].add(spk)
        return out

    ref_f, hyp_f = frames(reference), frames(hypothesis)
    overlap: dict[tuple[str, str], int] = {}
    for i in range(n):
        if excluded[i]:
            continue
        for r in ref_f[i]:
            for h in hyp_f[i]:
                overlap[(r, h)] = overlap.get((r, h), 0) + 1
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for (r, h), _ in sorted(overlap.items(), key=lambda kv: -kv[1]):
        if r not in mapping and h not in used:
            mapping[r] = h
            used.add(h)
    total = miss = fa = conf = 0
    for i in range(n):
        if excluded[i]:
            continue
        refs, hyps = ref_f[i], hyp_f[i]
        total += len(refs)
        mapped = {mapping.get(r) for r in refs}
        correct = len(mapped & hyps)
        miss += max(0, len(refs) - len(hyps))
        fa += max(0, len(hyps) - len(refs))
        conf += min(len(refs), len(hyps)) - correct
    total = max(total, 1)
    return {"der": (miss + fa + conf) / total, "miss": miss / total, "false_alarm": fa / total,
            "confusion": conf / total}
