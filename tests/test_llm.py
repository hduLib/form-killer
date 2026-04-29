from __future__ import annotations

import json

from form_killer.forms.yandex import parse_survey
from form_killer.llm.openai_answerer import LLMAnswerSet, OpenAIAnswerer, PersonalQuestionDecisionSet, build_prompt


def schema():
    return parse_survey(
        {
            "id": "s1",
            "name": "Sample",
            "pages": [
                {
                    "items": [
                        {"id": "name", "label": "Name", "type": "string", "validations": [{"type": "required"}]},
                        {
                            "id": "q1",
                            "label": "Pick one",
                            "type": "enum",
                            "widget": "radio",
                            "items": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                        },
                    ]
                }
            ],
        }
    )


class ParsedResponse:
    def __init__(self, parsed) -> None:
        self.output_parsed = parsed


class FakeResponses:
    def __init__(self) -> None:
        self.prompt = ""

    def parse(self, *, model: str, input: str, text_format: type[LLMAnswerSet]) -> ParsedResponse:
        self.prompt = input
        if text_format is PersonalQuestionDecisionSet:
            return ParsedResponse(
                PersonalQuestionDecisionSet.model_validate(
                    {"decisions": [{"question_id": "q1", "is_personal": True, "reason": "user-specific"}]}
                )
            )
        return ParsedResponse(
            LLMAnswerSet.model_validate(
                {
                    "answers": [
                        {
                            "question_id": "q1",
                            "option_ids": ["b", "not-real"],
                            "confidence": 0.9,
                            "rationale": "B fits.",
                            "needs_user_input": False,
                        }
                    ]
                }
            )
        )


class FakeClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


def test_openai_answerer_filters_invalid_options_and_personal_prompt() -> None:
    form = schema()
    fake_client = FakeClient()
    answerer = OpenAIAnswerer(api_key="test", client=fake_client)

    result = answerer.answer(form, form.questions)

    assert result.answers[0].option_ids == ["b"]
    assert "Name" not in fake_client.responses.prompt
    assert "Pick one" in fake_client.responses.prompt


def test_build_prompt_is_json() -> None:
    form = schema()
    prompt = build_prompt(form, [form.questions[1]])

    payload = json.loads(prompt)

    assert payload["form"]["id"] == "s1"
    assert payload["questions"][0]["options"][0] == {"id": "a", "label": "A"}


def test_openai_answerer_classifies_personal_questions() -> None:
    form = schema()
    fake_client = FakeClient()
    answerer = OpenAIAnswerer(api_key="test", client=fake_client)

    personal_ids = answerer.classify_personal_questions(form, [form.questions[1]])

    assert personal_ids == {"q1"}
