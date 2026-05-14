from __future__ import annotations

from form_killer.forms.browser_common import build_browser_form_schema, normalize_browser_question_kind
from form_killer.forms.schema import FORM_CONTROL_BINDINGS_KEY


def test_build_browser_form_schema_keeps_provider_specific_metadata() -> None:
    raw_questions = [
        {
            "id": "identity",
            "label": "Identity",
            "kind": "radio",
            "required": True,
            "dom_index": 0,
            "selector": "#identity",
            "options": [{"id": "1", "label": "Student"}],
        },
        {"id": "comment", "label": "Comment", "kind": "textarea", "required": False, "dom_index": 1},
    ]

    schema = build_browser_form_schema(
        raw_questions,
        source_url="https://docs.qq.com/form/page/demo",
        title="Demo",
        provider="tencent",
        raw_questions_key="tencent_questions",
        default_form_id="tencent-form",
    )

    assert schema.id == "demo"
    assert schema.raw["provider"] == "tencent"
    assert schema.raw["document_type"] == "form"
    assert schema.raw["source_url"] == "https://docs.qq.com/form/page/demo"
    assert schema.questions[0].kind == "enum"
    assert schema.questions[0].required is True
    assert schema.questions[0].options[0].label == "Student"
    assert schema.questions[1].kind == "text"
    assert schema.raw["tencent_questions"][0]["selector"] == "#identity"
    assert schema.raw["tencent_questions"][1]["dom_index"] == 1
    assert schema.raw[FORM_CONTROL_BINDINGS_KEY][0]["id"] == "identity"
    assert schema.raw[FORM_CONTROL_BINDINGS_KEY][0]["handle"] == "identity"
    assert schema.raw[FORM_CONTROL_BINDINGS_KEY][0]["selector"] == "#identity"


def test_normalize_browser_question_kind_uses_options_as_choice_fallback() -> None:
    assert normalize_browser_question_kind("textarea", []) == "text"
    assert normalize_browser_question_kind("scale", []) == "enum"
    assert normalize_browser_question_kind("unknown", [{"id": "1", "label": "A"}]) == "enum"
    assert normalize_browser_question_kind("unknown", []) == "unsupported"
