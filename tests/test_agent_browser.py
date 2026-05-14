from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from form_killer.forms.agent_browser import (
    BrowserFormToolExecutor,
    AgentBrowserFormService,
    AgentBrowserFormError,
    ResponsesToolLoop,
    parse_agent_browser_schema,
)
from form_killer.forms.schema import FORM_CONTROL_BINDINGS_KEY


def test_parse_agent_browser_schema_maps_questions_and_metadata() -> None:
    schema = parse_agent_browser_schema(
        {
            "id": "demo",
            "name": "Unknown Demo",
            "questions": [
                {"id": "name", "handle": "field_1", "label": "Name", "kind": "text", "required": True},
                {
                    "id": "choice",
                    "handle": "field_2",
                    "label": "Pick one",
                    "kind": "radio",
                    "options": [{"id": "a", "handle": "field_2_option_1", "label": "A"}],
                },
                {"id": "upload", "handle": "field_3", "label": "Upload", "kind": "file"},
            ],
        },
        source_url="https://example.test/form",
    )

    assert schema.raw["provider"] == "agent-browser"
    assert schema.raw["source_url"] == "https://example.test/form"
    assert schema.questions[0].kind == "string"
    assert schema.questions[1].kind == "enum"
    assert schema.questions[2].kind == "unsupported"
    assert schema.raw["agent_browser_questions"][1]["handle"] == "field_2"
    assert schema.raw["agent_browser_questions"][1]["options"][0]["handle"] == "field_2_option_1"
    assert schema.raw[FORM_CONTROL_BINDINGS_KEY][1]["handle"] == "field_2"


def test_responses_tool_loop_sends_function_call_output_with_call_id() -> None:
    calls: list[dict] = []

    class FakeResponses:
        def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(
                    id="resp_1",
                    output=[
                        SimpleNamespace(
                            type="function_call",
                            name="form_snapshot",
                            call_id="call_123",
                            arguments="{}",
                        )
                    ],
                )
            return SimpleNamespace(id="resp_2", output_text='{"ok": true}')

    class FakeExecutor:
        def call(self, name, arguments):
            assert name == "form_snapshot"
            assert arguments == {}
            return {"fields": []}

    loop = ResponsesToolLoop(
        client=SimpleNamespace(responses=FakeResponses()),
        model="gpt-test",
        executor=FakeExecutor(),
        max_steps=3,
    )

    assert loop.run("inspect") == {"ok": True}
    assert calls[1]["previous_response_id"] == "resp_1"
    assert calls[1]["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": json.dumps({"fields": []}, ensure_ascii=False),
        }
    ]


def test_browser_tool_executor_rejects_unknown_handles_and_dry_run_submit() -> None:
    class FakePage:
        def evaluate(self, script, arg=None):
            return {
                "url": "https://example.test/form",
                "title": "Demo",
                "fields": [
                    {
                        "handle": "field_1",
                        "label": "Name",
                        "kind": "text",
                        "options": [{"handle": "field_1_option_1", "label": "A"}],
                    }
                ],
                "buttons": [{"handle": "button_1", "label": "Submit", "kind": "submit"}],
            }

    executor = BrowserFormToolExecutor(FakePage(), allow_submit=False)
    snapshot = executor.form_snapshot()

    assert snapshot["fields"][0]["handle"] == "field_1"
    with pytest.raises(Exception, match="字段 handle"):
        executor.form_fill_field("field_missing", "Alice")
    with pytest.raises(Exception, match="禁止点击提交按钮"):
        executor.form_click("button_1")


def test_browser_tool_executor_accepts_question_handles_from_dom_schema_snapshot() -> None:
    class FakePage:
        def evaluate(self, script, arg=None):
            text = str(script)
            if "visible_text" in text:
                return {
                    "url": "https://example.test/form",
                    "title": "Demo",
                    "visible_text": "Name",
                    "dom_html": '<div data-form-killer-handle="field_question_1">Name<input /></div>',
                    "fields": [],
                    "questions": [{"handle": "field_question_1", "label": "Name", "kind": "input", "options": []}],
                    "buttons": [],
                }
            if "input.value" in text:
                return {"ok": True, "arg": arg}
            raise AssertionError("Unexpected script")

    executor = BrowserFormToolExecutor(FakePage(), allow_submit=False)
    snapshot = executor.form_snapshot()

    assert snapshot["dom_html"]
    assert executor.form_fill_field("field_question_1", "Alice")["ok"] is True


def test_agent_browser_submit_fills_from_schema_bindings_without_model() -> None:
    calls: list[tuple[str, object]] = []

    schema = parse_agent_browser_schema(
        {
            "id": "demo",
            "name": "Unknown Demo",
            "questions": [
                {"id": "name", "handle": "field_1", "label": "Name", "kind": "input"},
                {
                    "id": "choice",
                    "handle": "field_2",
                    "label": "Pick one",
                    "kind": "radio",
                    "options": [{"id": "a", "handle": "field_2_option_1", "label": "A"}],
                },
            ],
        },
        source_url="https://example.test/form",
    )

    class FakePage:
        def goto(self, url, **kwargs):
            calls.append(("goto", url))

        def evaluate(self, script, arg=None):
            text = str(script)
            if "fields, buttons" in text:
                calls.append(("snapshot", None))
                return {
                    "url": "https://example.test/form",
                    "title": "Demo",
                    "fields": [
                        {"handle": "field_1", "label": "Name", "kind": "input", "options": []},
                        {
                            "handle": "field_2",
                            "label": "Pick one",
                            "kind": "radio",
                            "options": [{"handle": "field_2_option_1", "id": "a", "label": "A"}],
                        },
                    ],
                    "buttons": [{"handle": "button_1", "label": "Submit", "kind": "submit"}],
                }
            if "input.value" in text:
                calls.append(("fill", arg))
                return {"ok": True}
            if "option.click" in text:
                calls.append(("choose", arg))
                return {"ok": True}
            raise AssertionError("Unexpected script")

    class FakeSession:
        page = FakePage()

        @classmethod
        def open(cls, *, storage_path, headless):
            return cls()

        def save_storage_state(self, path):
            pass

        def close(self):
            pass

    service = AgentBrowserFormService(browser_session_cls=FakeSession)
    result = service.submit(schema, {"name": "Alice", "choice": ["a"]}, dry_run=True)

    assert result["dryRun"] is True
    assert ("fill", {"handle": "field_1", "value": "Alice"}) in calls
    assert ("choose", {"handle": "field_2", "option_handle": "field_2_option_1"}) in calls


def test_agent_browser_opens_handoff_for_captcha_page_before_failing() -> None:
    calls: list[str] = []

    class CaptchaPage:
        url = "https://forms.yandex.ru/showcaptcha"

        def goto(self, url, **kwargs):
            calls.append("goto")

        def wait_for_selector(self, *args, **kwargs):
            pass

        def wait_for_load_state(self, *args, **kwargs):
            pass

        def wait_for_timeout(self, *args, **kwargs):
            pass

        def title(self):
            return "Вы не робот?"

        def locator(self, selector):
            class Locator:
                def inner_text(self, timeout=None):
                    return "Я не робот SmartCaptcha"

            return Locator()

    class FakeSession:
        page = CaptchaPage()

        @classmethod
        def open(cls, *, storage_path, headless):
            calls.append(f"open:{headless}")
            return cls()

        def save_storage_state(self, path):
            calls.append("save_storage")

        def close(self):
            calls.append("close")

    service = AgentBrowserFormService(
        browser_session_cls=FakeSession,
        handoff_waiter=lambda: calls.append("waiter"),
        max_handoff_attempts=2,
    )

    with pytest.raises(AgentBrowserFormError, match="验证码仍未完成"):
        service._browser_snapshot("https://forms.yandex.ru/u/demo")

    assert calls[:4] == ["open:True", "goto", "save_storage", "close"]
    assert "open:False" in calls
    assert calls.count("waiter") == 2


def test_agent_browser_handoff_reopens_visible_browser_and_continues() -> None:
    calls: list[str] = []
    visible_waited = False

    class Page:
        def __init__(self, captcha: bool):
            self.captcha = captcha
            self.url = "https://forms.yandex.ru/showcaptcha" if captcha else "https://forms.yandex.ru/u/demo"

        def goto(self, url, **kwargs):
            calls.append(f"goto:{self.captcha}")

        def wait_for_selector(self, *args, **kwargs):
            pass

        def wait_for_load_state(self, *args, **kwargs):
            pass

        def wait_for_timeout(self, *args, **kwargs):
            pass

        def title(self):
            return "Вы не робот?" if self.captcha and not visible_waited else "Demo"

        def locator(self, selector):
            captcha = self.captcha
            waited = visible_waited

            class Locator:
                def inner_text(self, timeout=None):
                    return "SmartCaptcha" if captcha and not waited else "Demo form"

            return Locator()

        def evaluate(self, script):
            return {"url": self.url, "title": "Demo", "fields": [], "buttons": []}

    class FakeSession:
        visible_session = None

        def __init__(self, captcha):
            self.page = Page(captcha)

        @classmethod
        def open(cls, *, storage_path, headless):
            calls.append(f"open:{headless}")
            session = cls(captcha=True)
            if not headless:
                cls.visible_session = session
            return session

        def close(self):
            calls.append("close")

        def save_storage_state(self, path):
            calls.append("save_storage")

    def handoff_waiter() -> None:
        nonlocal visible_waited
        visible_waited = True
        FakeSession.visible_session.page.url = "https://forms.yandex.ru/u/demo"
        calls.append("waiter")

    service = AgentBrowserFormService(
        browser_session_cls=FakeSession,
        handoff_waiter=handoff_waiter,
    )

    snapshot = service._browser_snapshot("https://forms.yandex.ru/u/demo")

    assert snapshot["title"] == "Demo"
    assert calls[:5] == ["open:True", "goto:True", "save_storage", "close", "open:False"]
    assert "waiter" in calls
