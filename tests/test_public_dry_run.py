from __future__ import annotations

import pytest

from form_killer.answers import missing_required
from form_killer.forms.base import Question
from form_killer.forms.yandex import YandexFormAdapter


PUBLIC_YANDEX_DRY_RUN_FORMS = [
    pytest.param(
        "https://forms.yandex.ru/u/68ee682502848fe7a5913682/",
        id="yandex-old-dominion-university",
    ),
    pytest.param(
        "https://forms.yandex.ru/u/6945481cf47e730fe4954bce/",
        id="yandex-public-23-questions",
    ),
    pytest.param(
        "https://forms.yandex.ru/u/676e79fa90fa7b176edb5182/",
        id="yandex-public-17-questions",
    ),
]


@pytest.mark.parametrize("url", PUBLIC_YANDEX_DRY_RUN_FORMS)
def test_public_yandex_form_accepts_dry_run(url: str) -> None:
    adapter = YandexFormAdapter(timeout=20)

    schema = adapter.fetch_schema(url)
    values = {question.id: value for question in schema.questions if (value := _dummy_value(question)) not in (None, "", [])}

    assert schema.questions
    assert missing_required(schema, values) == []

    response = adapter.submit(schema, values, dry_run=True)

    assert response.get("id") == schema.id
    assert response.get("answer_id") or response.get("answer_key") or response.get("integrations") is not None


def _dummy_value(question: Question) -> object | None:
    if question.kind in {"enum", "dropdown", "checkbox"}:
        return [question.options[0].id] if question.options else None
    if question.kind == "boolean":
        return True
    if question.kind == "integer":
        return 1
    if question.kind in {"string", "text"}:
        return "form-killer dry-run test"
    return None
