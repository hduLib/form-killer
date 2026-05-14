from __future__ import annotations

import json

from typer.testing import CliRunner

from form_killer import cli
from form_killer.forms.services import RoutedFormService
from form_killer.forms.tencent import TencentLoginRequired
from form_killer.forms.wps import WpsLoginRequired
from form_killer.forms.yandex import parse_survey
from form_killer.llm.openai_answerer import LLMAnswerSet
from form_killer.login import LoginEvent


runner = CliRunner()


class FakeProvider:
    def __init__(self, label: str) -> None:
        self.label = label


def test_supported_url_prompt_lists_registered_plugins(monkeypatch) -> None:
    monkeypatch.setattr(cli, "form_providers", lambda: (FakeProvider("Provider A"), FakeProvider("Provider B")))

    prompt = cli.supported_url_prompt()

    assert "当前可用表单插件" in prompt
    assert "- Provider A" in prompt
    assert "- Provider B" in prompt
    assert "请粘贴任一受支持的页面 URL" in prompt


def test_supported_url_prompt_handles_no_plugins(monkeypatch) -> None:
    monkeypatch.setattr(cli, "form_providers", lambda: ())

    assert "当前没有可用表单插件" in cli.supported_url_prompt()


def sample_schema():
    return parse_survey(
        {
            "id": "s1",
            "name": "CLI Sample",
            "texts": {"submit": "Send"},
            "pages": [
                {
                    "items": [
                        {"id": "name", "label": "Name", "type": "string", "validations": [{"type": "required"}]},
                        {
                            "id": "q1",
                            "label": "Pick one",
                            "type": "enum",
                            "widget": "radio",
                            "validations": [{"type": "required"}],
                            "items": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                        },
                    ]
                }
            ],
        }
    )


def schema_with_ai_personal_field():
    return parse_survey(
        {
            "id": "s2",
            "name": "CLI Personal Sample",
            "pages": [
                {
                    "items": [
                        {
                            "id": "assigned_code",
                            "label": "Your assigned lab code",
                            "type": "string",
                            "validations": [{"type": "required"}],
                        },
                        {
                            "id": "q1",
                            "label": "Pick one",
                            "type": "enum",
                            "widget": "radio",
                            "validations": [{"type": "required"}],
                            "items": [{"id": "a", "label": "A"}],
                        },
                    ]
                }
            ],
        }
    )


def test_cli_inspect_renders_schema(monkeypatch) -> None:
    class FakeAdapter:
        def fetch_schema(self, url: str):
            return sample_schema()

    monkeypatch.setattr(cli, "YandexFormAdapter", FakeAdapter)

    result = runner.invoke(cli.app, ["inspect", "https://example.test/u/s1/"])

    assert result.exit_code == 0
    assert "CLI Sample" in result.output
    assert "Pick one" in result.output


def test_cli_inspect_uses_agent_browser_fallback(monkeypatch) -> None:
    seen: dict[str, str | None] = {}

    class FakeAgentService:
        provider = "agent-browser"
        label = "Agent Loop 浏览器回退"
        document_type = "form"

        def configure_openai(self, *, api_key, base_url, model):
            seen["api_key"] = api_key
            seen["base_url"] = base_url
            seen["model"] = model

        def fetch_schema(self, url: str, event_handler=None):
            assert url == "https://example.test/form"
            return sample_schema()

    monkeypatch.setattr(
        cli,
        "resolve_form_service",
        lambda url: RoutedFormService(provider="agent-browser", document_type="form", service=FakeAgentService()),
    )

    result = runner.invoke(
        cli.app,
        [
            "inspect",
            "https://example.test/form",
            "--api-key",
            "test",
            "--base-url",
            "https://llm.example.test/v1",
            "--model",
            "gpt-test",
        ],
    )

    assert result.exit_code == 0
    assert seen == {"api_key": "test", "base_url": "https://llm.example.test/v1", "model": "gpt-test"}
    assert "CLI Sample" in result.output


def test_cli_inspect_agent_option_forces_agent_browser(monkeypatch) -> None:
    seen: list[str] = []

    class FakeAgentService:
        provider = "agent-browser"
        label = "Agent Loop 浏览器回退"
        document_type = "form"

        def configure_openai(self, *, api_key, base_url, model):
            seen.append(f"configure:{api_key}:{model}")

        def fetch_schema(self, url: str, event_handler=None):
            seen.append(f"fetch:{url}")
            return sample_schema()

    monkeypatch.setattr(cli, "AgentBrowserFormService", lambda: FakeAgentService())

    result = runner.invoke(
        cli.app,
        ["inspect", "https://docs.qq.com/form/page/demo", "--agent", "--api-key", "test", "--model", "gpt-test"],
    )

    assert result.exit_code == 0
    assert seen == ["configure:test:gpt-test", "fetch:https://docs.qq.com/form/page/demo"]
    assert "CLI Sample" in result.output


def test_cli_answer_dry_run_with_answers_file(monkeypatch, tmp_path) -> None:
    submitted: list[tuple[dict, bool]] = []

    class FakeAdapter:
        def fetch_schema(self, url: str):
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool):
            submitted.append((values, dry_run))
            return {"ok": True}

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            pass

        def classify_personal_questions(self, schema, questions):
            return set()

        def answer(self, schema, questions):
            return LLMAnswerSet.model_validate(
                {
                    "answers": [
                        {
                            "question_id": "q1",
                            "option_ids": ["b"],
                            "confidence": 0.95,
                            "rationale": "B is selected.",
                        }
                    ]
                }
            )

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"Name": "Alice"}), encoding="utf-8")
    monkeypatch.setattr(cli, "YandexFormAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)

    result = runner.invoke(
        cli.app,
        [
            "answer",
            "https://example.test/u/s1/",
            "--answers",
            str(answers_path),
            "--api-key",
            "test",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert submitted == [({"name": "Alice", "q1": ["b"]}, True)]
    assert "dry-run 校验通过" in result.output


def test_cli_answer_agent_browser_dry_run_does_not_submit(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, object]] = []

    class FakeAgentService:
        provider = "agent-browser"
        label = "Agent Loop 浏览器回退"
        document_type = "form"

        def configure_openai(self, *, api_key, base_url, model):
            calls.append(("configure", (api_key, base_url, model)))

        def fetch_schema(self, url: str, event_handler=None):
            calls.append(("fetch", url))
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool, event_handler=None):
            calls.append(("submit", (values, dry_run)))
            return {"ok": True, "dryRun": dry_run}

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            pass

        def classify_personal_questions(self, schema, questions):
            return set()

        def answer(self, schema, questions):
            return LLMAnswerSet.model_validate(
                {"answers": [{"question_id": "q1", "option_ids": ["b"], "confidence": 0.95}]}
            )

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"Name": "Alice"}), encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "resolve_form_service",
        lambda url: RoutedFormService(provider="agent-browser", document_type="form", service=FakeAgentService()),
    )
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)

    result = runner.invoke(
        cli.app,
        [
            "answer",
            "https://example.test/form",
            "--answers",
            str(answers_path),
            "--api-key",
            "test",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert ("submit", ({"name": "Alice", "q1": ["b"]}, True)) in calls
    assert "dry-run 校验通过" in result.output


def test_cli_answer_agent_option_forces_agent_browser(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    class FakeAgentService:
        provider = "agent-browser"
        label = "Agent Loop 浏览器回退"
        document_type = "form"

        def configure_openai(self, *, api_key, base_url, model):
            calls.append(f"configure:{api_key}")

        def fetch_schema(self, url: str, event_handler=None):
            calls.append(f"fetch:{url}")
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool, event_handler=None):
            calls.append(f"submit:{dry_run}:{values['name']}")
            return {"ok": True, "dryRun": dry_run}

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            pass

        def classify_personal_questions(self, schema, questions):
            return set()

        def answer(self, schema, questions):
            return LLMAnswerSet.model_validate(
                {"answers": [{"question_id": "q1", "option_ids": ["b"], "confidence": 0.95}]}
            )

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"Name": "Alice"}), encoding="utf-8")
    monkeypatch.setattr(cli, "AgentBrowserFormService", lambda: FakeAgentService())
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)

    result = runner.invoke(
        cli.app,
        [
            "answer",
            "https://docs.qq.com/form/page/demo",
            "--agent",
            "--answers",
            str(answers_path),
            "--api-key",
            "test",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        "configure:test",
        "fetch:https://docs.qq.com/form/page/demo",
        "submit:True:Alice",
    ]


def test_cli_answer_agent_browser_submit_requires_confirmation(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    class FakeAgentService:
        provider = "agent-browser"
        label = "Agent Loop 浏览器回退"
        document_type = "form"

        def configure_openai(self, *, api_key, base_url, model):
            pass

        def fetch_schema(self, url: str, event_handler=None):
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool, event_handler=None):
            calls.append(f"submit:{dry_run}")
            return {"ok": True}

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            pass

        def classify_personal_questions(self, schema, questions):
            return set()

        def answer(self, schema, questions):
            return LLMAnswerSet.model_validate(
                {"answers": [{"question_id": "q1", "option_ids": ["b"], "confidence": 0.95}]}
            )

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"Name": "Alice"}), encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "resolve_form_service",
        lambda url: RoutedFormService(provider="agent-browser", document_type="form", service=FakeAgentService()),
    )
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)

    cancelled = runner.invoke(
        cli.app,
        [
            "answer",
            "https://example.test/form",
            "--answers",
            str(answers_path),
            "--api-key",
            "test",
            "--submit",
        ],
        input="n\n",
    )
    assert cancelled.exit_code == 0
    assert calls == []

    confirmed = runner.invoke(
        cli.app,
        [
            "answer",
            "https://example.test/form",
            "--answers",
            str(answers_path),
            "--api-key",
            "test",
            "--submit",
            "--yes",
        ],
    )
    assert confirmed.exit_code == 0
    assert calls == ["submit:False"]


def test_cli_answer_prompts_for_url_when_missing(monkeypatch, tmp_path) -> None:
    submitted: list[tuple[dict, bool]] = []

    class FakeAdapter:
        def fetch_schema(self, url: str):
            assert url == "https://example.test/u/s1/"
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool):
            submitted.append((values, dry_run))
            return {"ok": True}

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            pass

        def classify_personal_questions(self, schema, questions):
            return set()

        def answer(self, schema, questions):
            return LLMAnswerSet.model_validate(
                {"answers": [{"question_id": "q1", "option_ids": ["a"], "confidence": 0.9}]}
            )

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"Name": "Alice"}), encoding="utf-8")
    monkeypatch.setattr(cli, "YandexFormAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)

    result = runner.invoke(
        cli.app,
        ["answer", "--answers", str(answers_path), "--api-key", "test", "--dry-run"],
        input="https://example.test/u/s1/\n",
    )

    assert result.exit_code == 0
    assert "腾讯文档" in result.output
    assert "CLI Sample" in result.output
    assert submitted == [({"name": "Alice", "q1": ["a"]}, True)]


def test_root_command_prompts_for_url_and_starts_answer(monkeypatch, tmp_path) -> None:
    class FakeAdapter:
        def fetch_schema(self, url: str):
            assert url == "https://example.test/u/s1/"
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool):
            raise AssertionError("No submit expected")

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            pass

        def classify_personal_questions(self, schema, questions):
            return set()

        def answer(self, schema, questions):
            return LLMAnswerSet.model_validate(
                {"answers": [{"question_id": "q1", "option_ids": ["b"], "confidence": 0.9}]}
            )

    monkeypatch.setattr(cli, "YandexFormAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(cli, "load_base_url", lambda: None)
    monkeypatch.setattr(cli, "load_model", lambda: None)

    result = runner.invoke(cli.app, [], input="https://example.test/u/s1/\n\n\nn\nAlice\nn\n")

    assert result.exit_code == 0
    assert "腾讯文档" in result.output
    assert "CLI Sample" in result.output


def test_cli_answer_passes_base_url_to_answerer(monkeypatch, tmp_path) -> None:
    seen: dict[str, str | None] = {}

    class FakeAdapter:
        def fetch_schema(self, url: str):
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool):
            return {"ok": True}

        def get_success(self, schema, answer_key: str):
            return {"answer_key": answer_key, "answer_id": 123}

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            seen["api_key"] = api_key
            seen["model"] = model
            seen["base_url"] = base_url

        def classify_personal_questions(self, schema, questions):
            return set()

        def answer(self, schema, questions):
            return LLMAnswerSet.model_validate(
                {"answers": [{"question_id": "q1", "option_ids": ["b"], "confidence": 0.9}]}
            )

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"Name": "Alice"}), encoding="utf-8")
    monkeypatch.setattr(cli, "YandexFormAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)

    result = runner.invoke(
        cli.app,
        [
            "answer",
            "https://example.test/u/s1/",
            "--answers",
            str(answers_path),
            "--api-key",
            "test-key",
            "--base-url",
            "https://llm.example.test/v1",
            "--model",
            "gpt-custom",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert seen == {"api_key": "test-key", "model": "gpt-custom", "base_url": "https://llm.example.test/v1"}


def test_cli_deletes_saved_key_after_authentication_error(monkeypatch, tmp_path) -> None:
    cleared: list[bool] = []

    class FakeAuthenticationError(Exception):
        pass

    class FakeAdapter:
        def fetch_schema(self, url: str):
            return sample_schema()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool):
            raise AssertionError("No submit expected")

        def get_success(self, schema, answer_key: str):
            raise AssertionError("No verification expected")

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            assert api_key == "bad-saved-key"

        def classify_personal_questions(self, schema, questions):
            raise FakeAuthenticationError("invalid api key")

        def answer(self, schema, questions):
            raise FakeAuthenticationError("invalid api key")

    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps({"Name": "Alice"}), encoding="utf-8")
    monkeypatch.setattr(cli, "YandexFormAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)
    monkeypatch.setattr(cli, "AuthenticationError", FakeAuthenticationError)
    monkeypatch.setattr(cli, "load_api_key_with_source", lambda: ("bad-saved-key", "config"))
    monkeypatch.setattr(cli, "clear_api_key", lambda: cleared.append(True) or ["config.json"])

    result = runner.invoke(
        cli.app,
        ["answer", "https://example.test/u/s1/", "--answers", str(answers_path)],
    )

    assert result.exit_code == 1
    assert cleared == [True]
    assert "OpenAI API Key" in result.output


def test_verify_submission_reports_answer_id(capsys) -> None:
    class FakeAdapter:
        def get_success(self, schema, answer_key: str):
            return {"answer_key": answer_key, "answer_id": 456}

    cli.verify_submission(FakeAdapter(), sample_schema(), "answer-key")

    captured = capsys.readouterr()
    assert "answer_id: 456" in captured.out


def test_fetch_schema_with_captcha_handoff_retries_with_browser_cookies(monkeypatch) -> None:
    calls: list[str] = []

    class FakeAdapter:
        def fetch_schema(self, url: str):
            calls.append(url)
            if len(calls) == 1:
                from form_killer.forms.yandex import YandexCaptchaError

                raise YandexCaptchaError(url)
            return sample_schema()

        def export_browser_cookies(self):
            return [{"name": "Session_id", "value": "before", "domain": ".yandex.ru", "path": "/"}]

        def import_browser_cookies(self, cookies):
            assert cookies == [{"name": "Session_id", "value": "after", "domain": ".yandex.ru", "path": "/"}]

    class FakeBrowserSession:
        @classmethod
        def open(cls, url, cookies):
            assert url == "https://example.test/u/s1/"
            assert cookies[0]["value"] == "before"
            return cls()

        def cookies(self):
            return [{"name": "Session_id", "value": "after", "domain": ".yandex.ru", "path": "/"}]

        def close(self):
            pass

    monkeypatch.setattr(cli, "BrowserCaptchaSession", FakeBrowserSession)
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: "")

    schema = cli.fetch_schema_with_captcha_handoff(
        FakeAdapter(),
        "https://example.test/u/s1/",
        allow_handoff=True,
    )

    assert schema.name == "CLI Sample"
    assert calls == ["https://example.test/u/s1/", "https://example.test/u/s1/"]


def test_cli_uses_ai_privacy_classification_for_user_input(monkeypatch) -> None:
    prompts: list[str] = []
    answered_question_ids: list[list[str]] = []

    class FakeAdapter:
        def fetch_schema(self, url: str):
            return schema_with_ai_personal_field()

        def upload_file(self, schema, question, path):
            raise AssertionError("No file upload expected")

        def submit(self, schema, values: dict, *, dry_run: bool):
            assert values == {"assigned_code": "LAB-7", "q1": ["a"]}
            return {"ok": True}

    class FakeAnswerer:
        def __init__(self, *, api_key: str, model: str, base_url: str | None = None):
            pass

        def classify_personal_questions(self, schema, questions):
            return {"assigned_code"}

        def answer(self, schema, questions):
            answered_question_ids.append([question.id for question in questions])
            return LLMAnswerSet.model_validate(
                {"answers": [{"question_id": "q1", "option_ids": ["a"], "confidence": 0.9}]}
            )

    def fake_prompt(prompt, *args, **kwargs):
        prompts.append(str(prompt))
        return "LAB-7"

    monkeypatch.setattr(cli, "YandexFormAdapter", FakeAdapter)
    monkeypatch.setattr(cli, "OpenAIAnswerer", FakeAnswerer)
    monkeypatch.setattr(cli.Prompt, "ask", fake_prompt)

    result = runner.invoke(
        cli.app,
        ["answer", "https://example.test/u/s2/", "--api-key", "test", "--dry-run"],
    )

    assert result.exit_code == 0
    assert any("assigned lab code" in prompt for prompt in prompts)
    assert answered_question_ids == [["q1"]]


def test_cli_tencent_login_handoff_retries_after_user_login(monkeypatch) -> None:
    calls: list[str] = []

    class FakeTencentService:
        provider = "tencent"
        document_type = "form"

        def fetch_schema(self, url: str):
            calls.append(f"fetch:{url}")
            if calls.count(f"fetch:{url}") == 1:
                raise TencentLoginRequired(url)
            return sample_schema()

        def login(self, url: str, event_handler=None, login_runner=None):
            calls.append(f"login:{url}")
            if event_handler:
                event_handler(LoginEvent("success"))

    monkeypatch.setattr(
        cli,
        "resolve_form_service",
        lambda url: RoutedFormService(provider="tencent", document_type="form", service=FakeTencentService()),
    )
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: "")
    result = runner.invoke(cli.app, ["inspect", "https://docs.qq.com/form/page/private", "--debug-browser"])

    assert result.exit_code == 0
    assert calls == [
        "fetch:https://docs.qq.com/form/page/private",
        "login:https://docs.qq.com/form/page/private",
        "fetch:https://docs.qq.com/form/page/private",
    ]
    assert "腾讯文档登录" in result.output
    assert "CLI Sample" in result.output


def test_cli_wps_login_handoff_retries_after_user_login(monkeypatch) -> None:
    calls: list[str] = []

    class FakeWpsService:
        provider = "wps"
        document_type = "form"

        def fetch_schema(self, url: str):
            calls.append(f"fetch:{url}")
            if calls.count(f"fetch:{url}") == 1:
                raise WpsLoginRequired(url)
            return sample_schema()

        def login(self, url: str, event_handler=None, login_runner=None):
            calls.append(f"login:{url}")
            if event_handler:
                event_handler(LoginEvent("success"))

    monkeypatch.setattr(
        cli,
        "resolve_form_service",
        lambda url: RoutedFormService(provider="wps", document_type="form", service=FakeWpsService()),
    )
    monkeypatch.setattr(cli.Prompt, "ask", lambda *args, **kwargs: "")
    result = runner.invoke(cli.app, ["inspect", "https://f.wps.cn/g/private", "--debug-browser"])

    assert result.exit_code == 0
    assert calls == [
        "fetch:https://f.wps.cn/g/private",
        "login:https://f.wps.cn/g/private",
        "fetch:https://f.wps.cn/g/private",
    ]
    assert "WPS/金山表单登录" in result.output
    assert "CLI Sample" in result.output


def test_cli_login_command_runs_tencent_login_challenge(monkeypatch) -> None:
    calls: list[str] = []

    class FakeLoginEvents:
        def __call__(self, event):
            calls.append(f"event:{event.kind}")

        def close(self):
            pass

    class FakeTencentService:
        provider = "tencent"
        document_type = "unsupported"

        def login(self, url: str, event_handler=None, login_runner=None):
            calls.append(f"login:{url}")
            if event_handler:
                event_handler(LoginEvent("success", login_name="Alice"))
            return "Alice"

    monkeypatch.setattr(
        cli,
        "resolve_form_service",
        lambda url: RoutedFormService(provider="tencent", document_type="unsupported", service=FakeTencentService()),
    )
    monkeypatch.setattr(cli, "run_cli_login_events", lambda *args, **kwargs: calls.append("renderer") or FakeLoginEvents())

    result = runner.invoke(cli.app, ["login", "https://docs.qq.com/"])

    assert result.exit_code == 0
    assert calls == ["renderer", "event:login_required", "login:https://docs.qq.com/", "event:success"]
    assert "Alice" in result.output


def test_cli_login_command_runs_wps_login_challenge(monkeypatch) -> None:
    calls: list[str] = []

    class FakeLoginEvents:
        def __call__(self, event):
            calls.append(f"event:{event.kind}")

        def close(self):
            pass

    class FakeWpsService:
        provider = "wps"
        document_type = "unsupported"

        def login(self, url: str, event_handler=None, login_runner=None):
            calls.append(f"login:{url}")
            if event_handler:
                event_handler(LoginEvent("success", login_name="Alice"))
            return "Alice"

    monkeypatch.setattr(
        cli,
        "resolve_form_service",
        lambda url: RoutedFormService(provider="wps", document_type="unsupported", service=FakeWpsService()),
    )
    monkeypatch.setattr(cli, "run_cli_login_events", lambda *args, **kwargs: calls.append("renderer") or FakeLoginEvents())

    result = runner.invoke(cli.app, ["login", "https://www.kdocs.cn/"])

    assert result.exit_code == 0
    assert calls == ["renderer", "event:login_required", "login:https://www.kdocs.cn/", "event:success"]
    assert "Alice" in result.output
