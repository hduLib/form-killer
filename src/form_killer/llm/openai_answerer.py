from __future__ import annotations

import json
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

from form_killer.forms.base import FormSchema, Question
from form_killer.privacy import is_personal_question


class LLMAnswer(BaseModel):
    question_id: str
    answer_value: str | int | bool | list[str] | None = None
    option_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    rationale: str = ""
    needs_user_input: bool = False


class LLMAnswerSet(BaseModel):
    answers: list[LLMAnswer]


class PersonalQuestionDecision(BaseModel):
    question_id: str
    is_personal: bool = False
    reason: str = ""


class PersonalQuestionDecisionSet(BaseModel):
    decisions: list[PersonalQuestionDecision]


class OpenAIAnswerer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5.5",
        base_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.client = client or OpenAI(api_key=api_key, base_url=base_url)

    def answer(self, schema: FormSchema, questions: list[Question]) -> LLMAnswerSet:
        safe_questions = [question for question in questions if not is_personal_question(question)]
        if not safe_questions:
            return LLMAnswerSet(answers=[])

        prompt = build_prompt(schema, safe_questions)
        parsed = self._parse_with_sdk(prompt)
        return validate_answer_set(parsed, safe_questions)

    def classify_personal_questions(self, schema: FormSchema, questions: list[Question]) -> set[str]:
        candidates = [question for question in questions if not is_personal_question(question)]
        if not candidates:
            return set()
        prompt = build_personal_classification_prompt(schema, candidates)
        parsed = self._parse_with_sdk(prompt, PersonalQuestionDecisionSet)
        return {
            decision.question_id
            for decision in parsed.decisions
            if decision.is_personal and any(question.id == decision.question_id for question in candidates)
        }

    def _parse_with_sdk(self, prompt: str, response_format: type[BaseModel] = LLMAnswerSet) -> Any:
        responses = self.client.responses
        if hasattr(responses, "parse"):
            result = responses.parse(
                model=self.model,
                input=prompt,
                text_format=response_format,
            )
            parsed = getattr(result, "output_parsed", None)
            if isinstance(parsed, response_format):
                return parsed

        result = responses.create(
            model=self.model,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": response_format.__name__,
                    "schema": response_format.model_json_schema(),
                    "strict": True,
                }
            },
        )
        output_text = getattr(result, "output_text", None) or _extract_output_text(result)
        try:
            return response_format.model_validate_json(output_text)
        except ValidationError:
            return response_format.model_validate(json.loads(output_text))


def build_prompt(schema: FormSchema, questions: list[Question]) -> str:
    payload = {
        "form": {"id": schema.id, "name": schema.name},
        "instructions": [
            "Answer only the provided non-personal form questions.",
            "For choice questions, return option_ids using the exact option id values.",
            "For uncertain, personal, identity, file, or context-dependent questions, set needs_user_input=true.",
            "Do not invent personal information.",
        ],
        "questions": [
            {
                "id": question.id,
                "label": question.label,
                "kind": question.kind,
                "required": question.required,
                "options": [option.model_dump() for option in question.options],
            }
            for question in questions
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_personal_classification_prompt(schema: FormSchema, questions: list[Question]) -> str:
    payload = {
        "form": {"id": schema.id, "name": schema.name},
        "task": "Classify whether each question asks for personal, private, identity, contact, account, location, file, or user-specific information that should be filled by the user instead of the AI.",
        "rules": [
            "Return is_personal=true when the answer depends on the user's identity, personal data, account, school/work ID, contact info, private file, variant/assigned number, address, birthday, or similar user-specific facts.",
            "Return is_personal=false for ordinary knowledge questions, quizzes, technical multiple choice, and questions answerable from public/common knowledge.",
            "Do not answer the questions. Only classify them.",
        ],
        "questions": [
            {
                "id": question.id,
                "label": question.label,
                "kind": question.kind,
                "required": question.required,
                "options": [option.model_dump() for option in question.options],
            }
            for question in questions
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def validate_answer_set(answer_set: LLMAnswerSet, questions: list[Question]) -> LLMAnswerSet:
    by_id = {question.id: question for question in questions}
    validated: list[LLMAnswer] = []
    for answer in answer_set.answers:
        question = by_id.get(answer.question_id)
        if not question:
            continue
        if question.is_choice:
            allowed = {option.id for option in question.options}
            answer.option_ids = [option_id for option_id in answer.option_ids if option_id in allowed]
            if not answer.option_ids:
                answer.needs_user_input = True
            if question.kind in {"enum", "dropdown"} and len(answer.option_ids) > 1:
                answer.option_ids = answer.option_ids[:1]
        validated.append(answer)
    return LLMAnswerSet(answers=validated)


def _extract_output_text(result: Any) -> str:
    chunks: list[str] = []
    for item in getattr(result, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                chunks.append(text)
    return "".join(chunks)
