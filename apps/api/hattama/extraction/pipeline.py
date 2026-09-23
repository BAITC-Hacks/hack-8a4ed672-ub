"""Bounded extraction pipeline:
event extraction (LLM, windowed with carried discussion state) -> linking -> normalisation ->
evidence verification -> questions for a human. Summary items are extracted per window and verified.

Retry policy: at most `max_format_retries` regenerations per window on invalid JSON/schema; then the
window error is recorded (visible review issue). No broad excepts, no infinite retries.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import ValidationError

from hattama.domain.names import ParticipantRef, norm, stem
from hattama.extraction import prompts
from hattama.extraction.evidence import SegmentView, numbers_supported, verify
from hattama.extraction.linker import LinkContext, Linker, TaskState, state_summary
from hattama.extraction.schemas import LlmEventsOut, LlmSummaryOut, llama_schema
from hattama.llm.client import LlamaCppClient, LlmOutputError, SamplingParams, parse_json_object

log = logging.getLogger(__name__)
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
PIPELINE_VERSION = "extract-v1"


@dataclass
class WindowStat:
    index: int
    segments: list[str]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    seconds: float = 0.0
    attempts: int = 0
    events: int = 0
    error: str | None = None
    kind: str = "events"


@dataclass
class ExtractionResult:
    cards: list[dict[str, Any]]
    summary: dict[str, Any]
    rejected_events: list[dict[str, Any]]
    window_errors: list[dict[str, Any]]
    stats: list[WindowStat] = field(default_factory=list)
    global_issues: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[TaskState] = field(default_factory=list)
    raw_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def llm_seconds(self) -> float:
        return round(sum(s.seconds for s in self.stats), 2)


@dataclass
class PipelineConfig:
    ctx_size: int = 4096
    max_tokens: int = 1000
    window_chars: int = 2400
    context_segments: int = 2
    max_format_retries: int = 1
    with_summary: bool = True
    sampling: SamplingParams = field(default_factory=SamplingParams)


class ExtractionPipeline:
    def __init__(self, client: LlamaCppClient, config: PipelineConfig | None = None) -> None:
        self.client = client
        self.cfg = config or PipelineConfig()
        self._events_schema = llama_schema(LlmEventsOut)
        self._summary_schema = llama_schema(LlmSummaryOut)

    # ------------------------------------------------------------------ prompt pieces
    @staticmethod
    def meeting_line(meeting_date: date, timezone: str, title: str | None = None) -> str:
        head = f"Совещание «{title}». " if title else ""
        return f"{head}Дата совещания: {meeting_date.isoformat()} ({WEEKDAYS[meeting_date.weekday()]}), " \
               f"часовой пояс {timezone}."

    @staticmethod
    def participants_line(participants: list[ParticipantRef]) -> str:
        items = []
        for p in participants:
            tag = "подразделение" if p.kind == "department" else ("присутствует" if p.is_present else "не присутствует")
            items.append(f"{p.display_name} ({tag})")
        return "; ".join(items)[:600] or "(список не задан)"

    @staticmethod
    def speaker_of(seg: SegmentView) -> str:
        return seg.speaker_name or seg.speaker_label or "Говорящий не определён"

    def _windows(self, ordered: list[SegmentView], start: int) -> list[list[SegmentView]]:
        windows: list[list[SegmentView]] = []
        cur: list[SegmentView] = []
        size = 0
        for seg in ordered[start:]:
            line = len(seg.text) + 40
            if cur and size + line > self.cfg.window_chars:
                windows.append(cur)
                cur, size = [], 0
            cur.append(seg)
            size += line
        if cur:
            windows.append(cur)
        return windows

    def _fit(self, system: str, build: Callable[[int], str], n: int, extra: str = "") -> tuple[str, int]:
        """Shrink the window (drop trailing segments) until the prompt fits the context budget."""
        budget = self.cfg.ctx_size - self.cfg.max_tokens - 64
        while n > 0:
            user = build(n)
            tokens = self.client.count_tokens(system + "\n" + extra + "\n" + user) + 60  # chat template overhead
            if tokens <= budget:
                return user, n
            n -= 1
        raise LlmOutputError("один сегмент не помещается в контекст модели")

    def _call(self, system: str, user: str, schema: dict[str, Any], model: type, stat: WindowStat,
              example: tuple[str, str] | None = None) -> Any:
        messages = [{"role": "system", "content": system}]
        if example:
            messages += [{"role": "user", "content": example[0]}, {"role": "assistant", "content": example[1]}]
        messages.append({"role": "user", "content": user})
        last_error = None
        for attempt in range(self.cfg.max_format_retries + 1):
            stat.attempts = attempt + 1
            call = self.client.chat_json(messages, schema, self.cfg.sampling)
            stat.seconds += call.seconds
            stat.prompt_tokens = call.prompt_tokens
            stat.completion_tokens = (stat.completion_tokens or 0) + (call.completion_tokens or 0)
            try:
                if call.finish_reason == "length":
                    raise LlmOutputError("ответ обрезан по лимиту токенов")
                return model.model_validate(parse_json_object(call.content))
            except (LlmOutputError, ValidationError) as exc:
                last_error = str(exc)[:500]
                messages = [*messages, {"role": "assistant", "content": call.content[:2000]},
                            {"role": "user", "content": f"Ответ не соответствует схеме: {last_error}. "
                                                        "Верни исправленный JSON целиком, короче."}]
        raise LlmOutputError(last_error or "ошибка формата")

    # ------------------------------------------------------------------ main
    def run(self, *, meeting_date: date, timezone: str, title: str | None, participants: list[ParticipantRef],
            ordered: list[SegmentView], existing: list[TaskState] | None = None, start_index: int = 0,
            on_window: Callable[[int, int], None] | None = None) -> ExtractionResult:
        for i, seg in enumerate(ordered):
            seg.order = i
        by_alias = {s.alias: s for s in ordered}
        link_ctx = LinkContext(meeting_date, timezone, participants, by_alias, ordered)
        linker = Linker(link_ctx, existing)
        meeting_line = self.meeting_line(meeting_date, timezone, title)
        part_line = self.participants_line(participants)
        stats: list[WindowStat] = []
        window_errors: list[dict[str, Any]] = []
        summary_items: list[dict[str, Any]] = []
        raw_events: list[dict[str, Any]] = []
        topics: list[str] = []
        windows = self._windows(ordered, start_index)
        pos = start_index
        idx = 0
        w_no = 0
        while pos < len(ordered):
            window = self._windows(ordered, pos)[0]
            context = ordered[max(0, pos - self.cfg.context_segments):pos]
            ctx_lines = [prompts.format_segment_line(s.alias, self.speaker_of(s), s.text) for s in context]
            state = state_summary(list(linker.tasks.values()))

            def build(n: int, window: list[SegmentView] = window, ctx_lines: list[str] = ctx_lines,
                      state: str = state) -> str:
                lines = [prompts.format_segment_line(s.alias, self.speaker_of(s), s.text) for s in window[:n]]
                return prompts.events_user_message(meeting_line, part_line, state, ctx_lines, lines)

            stat = WindowStat(w_no, [s.id for s in window])
            try:
                example = (prompts.EVENTS_EXAMPLE_USER, prompts.EVENTS_EXAMPLE_ASSISTANT)
                user, n = self._fit(prompts.EVENTS_SYSTEM, build, len(window), extra="\n".join(example))
                window = window[:n]
                stat.segments = [s.id for s in window]
                out: LlmEventsOut = self._call(prompts.EVENTS_SYSTEM, user, self._events_schema, LlmEventsOut, stat,
                                               example=example)
                linker.begin_window()
                allowed = {s.alias for s in window}
                raw_events += [{"window": w_no, **e.model_dump(exclude_none=True)} for e in out.events]
                for e in out.events:
                    if e.segment not in allowed:
                        linker.rejected_events.append({"event_index": idx, "type": e.type,
                                                       "reason": f"событие вне окна ({e.segment})"})
                    else:
                        linker.apply(e, idx)
                    idx += 1
                stat.events = len(out.events)
            except LlmOutputError as exc:
                stat.error = str(exc)
                window_errors.append({"window": w_no, "segments": stat.segments, "error": str(exc), "kind": "events"})
            stats.append(stat)
            if self.cfg.with_summary:
                sstat = WindowStat(w_no, stat.segments, kind="summary")
                lines = [prompts.format_segment_line(s.alias, self.speaker_of(s), s.text) for s in window]
                try:
                    out_s: LlmSummaryOut = self._call(prompts.SUMMARY_SYSTEM,
                                                      prompts.summary_user_message(meeting_line, lines),
                                                      self._summary_schema, LlmSummaryOut, sstat)
                    allowed = {s.alias for s in window}
                    topics += out_s.topics
                    for item in out_s.items:
                        summary_items.append(self._verify_summary_item(item.model_dump(), by_alias, ordered, allowed))
                except LlmOutputError as exc:
                    sstat.error = str(exc)
                    window_errors.append({"window": w_no, "segments": stat.segments, "error": str(exc),
                                          "kind": "summary"})
                stats.append(sstat)
            pos += len(window)
            w_no += 1
            if on_window:
                on_window(w_no, len(windows))
        return ExtractionResult(cards=linker.cards(), summary=merge_summary(summary_items, topics),
                                rejected_events=linker.rejected_events, window_errors=window_errors, stats=stats,
                                global_issues=[i.__dict__ for i in linker.global_issues],
                                tasks=list(linker.tasks.values()), raw_events=raw_events)

    @staticmethod
    def _verify_summary_item(item: dict[str, Any], by_alias: dict[str, SegmentView], ordered: list[SegmentView],
                             allowed: set[str]) -> dict[str, Any]:
        flags = []
        if item["segment"] not in allowed:
            flags.append("ссылка на сегмент вне окна")
        ev = verify(item["quote"], item["segment"], by_alias, ordered)
        if ev.match == "missing":
            flags.append("цитата не найдена в тексте")
        seg_text = by_alias[ev.alias].text if ev.alias in by_alias else ""
        unsupported = numbers_supported(item["text"], seg_text)
        if unsupported:
            flags.append(f"числа не найдены в основании: {', '.join(unsupported)}")
        flags += grounding_flags(item["category"], item["text"], item["quote"])
        return {"category": item["category"], "text": item["text"], "evidence": [ev.to_dict()], "flags": flags}


MODAL_MARKERS = ("скорее всего", "возможно", "наверное", "вероятно", "думаю", "кажется", "по-видимому", "может быть",
                 "предполага", "видимо", "мүмкін", "шамасы", "меніңше", "сияқты", "болуы мүмкін")


def grounding_flags(category: str, text: str, quote: str) -> list[str]:
    """Deterministic checks that a summary statement does not exceed its quote."""
    flags = []
    t_stems = {stem(w) for w in norm(text).split() if len(w) > 3}
    q_stems = {stem(w) for w in norm(quote).split() if len(w) > 3}
    if t_stems and len(t_stems & q_stems) / len(t_stems) < 0.4:
        flags.append("утверждение шире цитаты — проверьте, не добавлено ли лишнее")
    modal_in_quote = any(m in quote.lower() for m in MODAL_MARKERS)
    if category == "fact" and modal_in_quote:
        flags.append("факт основан на предположении участника")
    if category == "assumption" and not modal_in_quote and not any(m in text.lower() for m in MODAL_MARKERS):
        flags.append("предположение без модальности в цитате — проверьте категорию")
    return flags


def merge_summary(items: list[dict[str, Any]], topics: list[str]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {"fact": [], "decision": [], "assumption": [], "risk": [],
                                                 "open_question": []}
    seen: set[str] = set()
    for item in items:
        key = item["category"] + "|" + norm(item["text"])
        if key in seen:
            continue
        seen.add(key)
        buckets[item["category"]].append(item)
    uniq_topics = list(dict.fromkeys(t.strip() for t in topics if t.strip()))[:10]
    return {"topics": uniq_topics, "facts": buckets["fact"], "decisions": buckets["decision"],
            "assumptions": buckets["assumption"], "risks": buckets["risk"],
            "open_questions": buckets["open_question"], "generated_by": PIPELINE_VERSION,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
