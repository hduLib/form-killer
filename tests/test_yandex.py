from __future__ import annotations

import json
from pathlib import Path

import httpx

import pytest

from form_killer.forms.yandex import YandexCaptchaError, YandexFormAdapter, extract_survey_id, parse_survey


FIXTURE = Path(__file__).parent / "fixtures" / "yandex_get_survey.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_survey_fixture() -> None:
    schema = parse_survey(load_fixture())

    assert schema.id == "69e4f367d04688b72f93623c"
    assert schema.name == "ES-S2026 LW4 Report"
    assert len(schema.questions) == 6
    assert schema.questions[0].required is True
    assert schema.questions[3].kind == "file"
    assert schema.questions[3].max_file_count == 1
    assert schema.questions[3].max_file_size_mb == 20
    assert schema.questions[4].kind == "enum"
    assert schema.questions[4].options[0].id == "142523260"


def test_extract_survey_id_from_url() -> None:
    assert (
        extract_survey_id("https://forms.yandex.ru/u/69e4f367d04688b72f93623c/")
        == "69e4f367d04688b72f93623c"
    )


def test_fetch_and_submit_payloads_via_mock_transport() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            html = (
                '<script>window.__DATA__ = {"lang":"ru","sdkEndpoint":"/u/gateway",'
                '"csrfToken":"token:123","user":{"yandexuid":"uid"}};</script>'
            )
            return httpx.Response(200, text=html)
        if request.url.path.endswith("/root/form/getSurvey"):
            assert json.loads(request.content) == {"surveyId": "69e4f367d04688b72f93623c"}
            assert request.headers["x-csrf-token"] == "token:123"
            return httpx.Response(200, json=load_fixture())
        if request.url.path.endswith("/root/form/postSurvey"):
            payload = json.loads(request.content)
            assert payload["surveyId"] == "69e4f367d04688b72f93623c"
            assert payload["dryRun"] is True
            assert payload["values"]["answer_choices_71845799"] == ["142523260"]
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/root/form/getSuccess"):
            payload = json.loads(request.content)
            assert payload == {
                "surveyId": "69e4f367d04688b72f93623c",
                "answerKey": "answer-key",
            }
            return httpx.Response(200, json={"answer_key": "answer-key", "answer_id": 123})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    adapter = YandexFormAdapter(client=client)

    schema = adapter.fetch_schema("https://forms.yandex.ru/u/69e4f367d04688b72f93623c/")
    response = adapter.submit(schema, {"answer_choices_71845799": ["142523260"]}, dry_run=True)
    success = adapter.get_success(schema, "answer-key")

    assert response == {"ok": True}
    assert success == {"answer_key": "answer-key", "answer_id": 123}
    assert [request.url.path for request in requests] == [
        "/u/69e4f367d04688b72f93623c/",
        "/u/gateway/root/form/getSurvey",
        "/u/gateway/root/form/postSurvey",
        "/u/gateway/root/form/getSuccess",
    ]


def test_fetch_schema_reports_captcha_without_bypass() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<!doctype html><link rel="stylesheet" href="/captcha_smart.css">')

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    adapter = YandexFormAdapter(client=client)

    with pytest.raises(YandexCaptchaError) as exc_info:
        adapter.fetch_schema("https://forms.yandex.ru/u/69e4f367d04688b72f93623c/")

    assert exc_info.value.url == "https://forms.yandex.ru/u/69e4f367d04688b72f93623c/"
