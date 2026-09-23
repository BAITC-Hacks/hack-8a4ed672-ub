"""Structured LLM output contracts (validated by Pydantic; also sent as JSON Schema grammar to llama.cpp).

Valid JSON is not proof of truth: every reference and quote is re-checked by hattama.extraction.evidence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "assign",           # постановка задачи (или уточнение полей существующей)
    "propose",          # предложение / вопрос («Две недели достаточно?») — ещё не согласовано
    "accept",           # принятие предложения
    "reject",           # отказ / возражение
    "change_deadline",  # согласованное изменение срока
    "change_assignee",  # смена исполнителя
    "cancel",           # отмена поручения
    "suspend",          # «пока не выполнять», приостановка
    "conditional",      # условное решение («если …, то …»)
    "restate",          # повтор / итоговое резюме ранее сказанного
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LlmEvent(_Strict):
    # Field order matters for generation: grounding quote first, then the abstraction.
    type: EventType
    segment: str = Field(pattern=r"^S[0-9]{1,4}$", description="сегмент, где произошло событие")
    task: str = Field(pattern=r"^(T|N)[0-9]{1,3}$", description="T<n> — существующая задача, N<n> — новая")
    action_quote: str | None = Field(default=None, max_length=200, description="точные слова о самой задаче")
    action: str | None = Field(default=None, max_length=200, description="что сделать, в инфинитиве; только для новой задачи")
    assignee: str | None = Field(default=None, max_length=120)
    assignee_kind: Literal["person", "department", "group", "unknown"] | None = None
    assignee_quote: str | None = Field(default=None, max_length=160)
    deadline_quote: str | None = Field(default=None, max_length=160, description="точная формулировка срока")
    condition_quote: str | None = Field(default=None, max_length=240)
    parent: str | None = Field(default=None, pattern=r"^(T|N)[0-9]{1,3}$")
    curator: str | None = Field(default=None, max_length=120)
    assigner: str | None = Field(default=None, max_length=120)
    collaborators: list[str] = Field(default_factory=list, max_length=5)
    expected_result: str | None = Field(default=None, max_length=200)
    depends_on: list[str] = Field(default_factory=list, max_length=5)
    topic: str | None = Field(default=None, max_length=120)


class LlmEventsOut(_Strict):
    events: list[LlmEvent] = Field(default_factory=list, max_length=24)


class LlmSummaryItem(_Strict):
    category: Literal["fact", "decision", "assumption", "risk", "open_question"]
    segment: str = Field(pattern=r"^S[0-9]{1,4}$")
    quote: str = Field(max_length=300)
    text: str = Field(max_length=240)


class LlmSummaryOut(_Strict):
    topics: list[str] = Field(default_factory=list, max_length=8)
    items: list[LlmSummaryItem] = Field(default_factory=list, max_length=12)


def llama_schema(model: type[BaseModel]) -> dict:
    """JSON Schema for llama.cpp grammar: inline $defs (maximally compatible), drop descriptions."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):  # type: ignore[no-untyped-def]
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(defs[node["$ref"].split("/")[-1]])
            return {k: resolve(v) for k, v in node.items() if k not in ("description", "title", "default")}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)
