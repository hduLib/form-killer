from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from form_killer.forms.base import AnswerPayload, FormSchema, Question
from form_killer.llm.openai_answerer import LLMAnswerSet
from form_killer.privacy import is_personal_question


class AnswerError(RuntimeError):
    pass


def load_answer_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise AnswerError(f"Answers file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AnswerError(f"Answers file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise AnswerError("Answers file must be a JSON object mapping question ids or labels to values.")
    return data


def question_needs_user(question: Question) -> bool:
    return question.required and (is_personal_question(question) or question.kind in {"file", "unsupported"})


def answer_from_mapping(question: Question, mapping: dict[str, Any]) -> AnswerPayload | None:
    sentinel = object()
    raw = mapping.get(question.id, sentinel)
    if raw is sentinel:
        raw = mapping.get(question.label, sentinel)
    if raw is sentinel:
        return None
    return AnswerPayload(
        question_id=question.id,
        value=normalize_value(question, raw),
        source="file" if question.kind == "file" else "user",
    )


def llm_answers_to_payloads(answer_set: LLMAnswerSet, schema: FormSchema) -> list[AnswerPayload]:
    questions = {question.id: question for question in schema.questions}
    payloads: list[AnswerPayload] = []
    for answer in answer_set.answers:
        question = questions.get(answer.question_id)
        if question is None:
            continue
        value: Any
        if question.is_choice:
            value = answer.option_ids
        else:
            value = answer.answer_value
        payloads.append(
            AnswerPayload(
                question_id=answer.question_id,
                value=value,
                source="llm",
                confidence=answer.confidence,
                rationale=answer.rationale,
                needs_user_input=answer.needs_user_input or answer.confidence < 0.45,
            )
        )
    return payloads


def normalize_value(question: Question, raw: Any) -> Any:
    if question.is_choice:
        return normalize_choice_value(question, raw)
    if question.kind == "integer":
        return int(raw)
    if question.kind == "boolean":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "да"}
    if question.kind == "file":
        return str(raw)
    return str(raw)


def normalize_choice_value(question: Question, raw: Any) -> list[str]:
    values = raw if isinstance(raw, list) else [raw]
    by_id = {option.id: option.id for option in question.options}
    by_slug = {slug_text(option.id): option.id for option in question.options}
    by_label = {option.label.strip().lower(): option.id for option in question.options}
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        option_id = by_id.get(text) or by_slug.get(slug_text(text)) or by_label.get(text.lower())
        if not option_id:
            raise AnswerError(f"Unknown option for '{question.label}': {value}")
        result.append(option_id)
    if question.kind in {"enum", "dropdown"}:
        return result[:1]
    return result


def slug_text(value: str) -> str:
    return "".join("_" if not char.isalnum() else char.lower() for char in value).strip("_")


def build_values(schema: FormSchema, answers: list[AnswerPayload]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    question_ids = {question.id for question in schema.questions}
    for answer in answers:
        if answer.question_id in question_ids and answer.value not in (None, "", []):
            values[answer.question_id] = answer.value
    return values


def missing_required(schema: FormSchema, values: dict[str, Any]) -> list[Question]:
    missing: list[Question] = []
    for question in schema.questions:
        if question.required and question.id not in values:
            missing.append(question)
    return missing
