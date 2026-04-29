from __future__ import annotations

from form_killer.answers import answer_from_mapping, build_values, llm_answers_to_payloads, missing_required
from form_killer.forms.base import AnswerPayload
from form_killer.forms.yandex import parse_survey
from form_killer.llm.openai_answerer import LLMAnswerSet


def test_answer_mapping_accepts_labels_for_choices() -> None:
    schema = parse_survey(
        {
            "id": "s1",
            "name": "Sample",
            "pages": [
                {
                    "items": [
                        {
                            "id": "q1",
                            "label": "Pick one",
                            "type": "enum",
                            "widget": "radio",
                            "items": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                        }
                    ]
                }
            ],
        }
    )

    answer = answer_from_mapping(schema.questions[0], {"Pick one": "B"})

    assert answer is not None
    assert answer.value == ["b"]


def test_low_confidence_llm_answer_is_not_merged_into_values() -> None:
    schema = parse_survey(
        {
            "id": "s1",
            "name": "Sample",
            "pages": [
                {
                    "items": [
                        {
                            "id": "q1",
                            "label": "Required choice",
                            "type": "enum",
                            "widget": "radio",
                            "validations": [{"type": "required"}],
                            "items": [{"id": "a", "label": "A"}],
                        }
                    ]
                }
            ],
        }
    )
    answer_set = LLMAnswerSet.model_validate(
        {"answers": [{"question_id": "q1", "option_ids": ["a"], "confidence": 0.2, "rationale": "guess"}]}
    )

    payloads = llm_answers_to_payloads(answer_set, schema)
    usable = [payload for payload in payloads if not payload.needs_user_input]
    values = build_values(schema, usable)

    assert missing_required(schema, values) == [schema.questions[0]]


def test_build_values_skips_empty_values() -> None:
    schema = parse_survey(
        {"id": "s1", "name": "Sample", "pages": [{"items": [{"id": "q1", "label": "Text", "type": "string"}]}]}
    )

    values = build_values(schema, [AnswerPayload(question_id="q1", value="hello")])

    assert values == {"q1": "hello"}
