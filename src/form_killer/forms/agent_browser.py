from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4
from typing import Any

from openai import BadRequestError, OpenAI
from pydantic import ValidationError

from form_killer.browser import BrowserLaunchError, launch_chromium
from form_killer.config import PROJECT_CACHE_DIR_NAME
from form_killer.events import ServiceEvent, ServiceEventHandler
from form_killer.forms.base import FormSchema, FormServiceError, Option, Question
from form_killer.forms.browser_common import extract_form_id, normalize_browser_question_kind
from form_killer.forms.schema import (
    FORM_CONTROL_BINDINGS_KEY,
    build_control_binding,
    control_bindings_by_question_id,
    option_handle_for_value,
)


AGENT_BROWSER_PROVIDER = "agent-browser"
AGENT_BROWSER_LABEL = "Agent Loop 浏览器回退"
AGENT_BROWSER_QUESTIONS_KEY = "agent_browser_questions"
DEFAULT_AGENT_BROWSER_MAX_STEPS = 20


class AgentBrowserFormError(FormServiceError):
    pass


class AgentBrowserSession:
    def __init__(self, playwright: Any, browser: Any, context: Any, page: Any) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self.page = page

    @classmethod
    def open(cls, *, storage_path: Path | None, headless: bool) -> "AgentBrowserSession":
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise AgentBrowserFormError("缺少 Playwright，无法打开 Agent Loop 浏览器回退。") from exc

        playwright = sync_playwright().start()
        try:
            browser = launch_chromium(playwright, headless=headless)
            kwargs: dict[str, Any] = {}
            if storage_path is not None and storage_path.exists():
                kwargs["storage_state"] = str(storage_path)
            context = browser.new_context(**kwargs)
            page = context.new_page()
        except BrowserLaunchError as exc:
            playwright.stop()
            raise AgentBrowserFormError(str(exc)) from exc
        except Exception:
            playwright.stop()
            raise
        return cls(playwright, browser, context, page)

    def save_storage_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(path))

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._playwright.stop()


FORM_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "form_snapshot",
        "description": "Read visible form fields, options, allowed form navigation or submit buttons, and sanitized DOM context for schema inference.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "form_fill_field",
        "description": "Fill a text, number, or textarea field by handle from form_snapshot.",
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "value": {"type": ["string", "number", "boolean"]},
            },
            "required": ["handle", "value"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "form_choose_option",
        "description": "Choose one option for a radio, dropdown, rating, or other single-choice field.",
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "option_handle": {"type": "string"},
            },
            "required": ["handle", "option_handle"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "form_set_checkbox_group",
        "description": "Set checkbox/multi-select options for a field using option handles from form_snapshot.",
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "option_handles": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["handle", "option_handles"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "form_click",
        "description": "Click a form navigation or submit button from form_snapshot.",
        "parameters": {
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "form_wait",
        "description": "Wait for the page to stabilize after form interaction.",
        "parameters": {
            "type": "object",
            "properties": {"milliseconds": {"type": "integer", "minimum": 100, "maximum": 10000}},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "form_read_status",
        "description": "Read visible success or failure status text after filling or submitting.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class AgentBrowserFormService:
    provider = AGENT_BROWSER_PROVIDER
    label = AGENT_BROWSER_LABEL
    document_type = "form"
    supports_login = False
    login_required_error = None

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        session_path: Path | None = None,
        headless: bool = True,
        browser_session_cls: type[AgentBrowserSession] = AgentBrowserSession,
        max_steps: int = DEFAULT_AGENT_BROWSER_MAX_STEPS,
        persist_session: bool = True,
        allow_handoff: bool = True,
        handoff_waiter: Any | None = None,
        max_handoff_attempts: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model or "gpt-5.5"
        self.client = client
        self.session_path = session_path or Path.cwd() / PROJECT_CACHE_DIR_NAME / "sessions" / "agent-browser.json"
        self.headless = headless
        self.browser_session_cls = browser_session_cls
        self.max_steps = max_steps
        self.persist_session = persist_session
        self.allow_handoff = allow_handoff
        self.handoff_waiter = handoff_waiter
        self.max_handoff_attempts = max_handoff_attempts

    def configure_openai(self, *, api_key: str | None, base_url: str | None, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        if self.client is None and not api_key:
            return
        if self.client is None and api_key:
            self.client = OpenAI(api_key=api_key, base_url=base_url)

    def configure_handoff(self, *, allow_handoff: bool, handoff_waiter: Any | None = None) -> None:
        self.allow_handoff = allow_handoff
        self.handoff_waiter = handoff_waiter

    def fetch_schema(self, url: str, event_handler: ServiceEventHandler | None = None) -> FormSchema:
        if event_handler:
            event_handler(ServiceEvent("parse_start", provider=self.provider, provider_label=self.label))
        result = self._run_browser_agent(url, build_schema_prompt(url), allow_submit=False)
        schema_payload = result.get("schema") if isinstance(result.get("schema"), dict) else result
        return parse_agent_browser_schema(schema_payload, source_url=url)

    def login(self, url: str, **_: Any) -> str | None:
        raise AgentBrowserFormError("Agent Loop 浏览器回退不处理登录；请先在浏览器中完成登录或使用预设 provider。")

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        raise AgentBrowserFormError("Agent Loop 浏览器回退 v1 暂不支持文件上传字段。")

    def submit(
        self,
        schema: FormSchema,
        values: dict[str, Any],
        *,
        dry_run: bool,
        event_handler: ServiceEventHandler | None = None,
    ) -> dict[str, Any]:
        if event_handler:
            event_handler(ServiceEvent("dry_run_start" if dry_run else "fill_start", provider=self.provider, provider_label=self.label))
        source_url = str(schema.raw.get("source_url") or "")
        if not source_url:
            raise AgentBrowserFormError("缺少未知表单来源 URL，无法执行 Agent Loop 浏览器回填。")
        result = self._fill_schema_values(source_url, schema, values, allow_submit=not dry_run)
        if dry_run:
            return {"ok": True, "dryRun": True, "provider": self.provider, "agent": result}
        if result.get("ok") is False:
            raise AgentBrowserFormError(str(result.get("error") or "Agent Loop 浏览器提交失败。"))
        return {"ok": True, "provider": self.provider, "agent": result}

    def verify_submission(self, schema: FormSchema, answer_key: str) -> dict[str, Any]:
        return {}

    def _run_browser_agent(self, url: str, prompt: str, *, allow_submit: bool) -> dict[str, Any]:
        if self.client is None:
            if not self.api_key:
                raise AgentBrowserFormError(
                    "未命中预设 provider，已尝试 Agent Loop 浏览器回退；该回退需要 OpenAI API Key。"
                    "请使用 OPENAI_API_KEY、--api-key，或 form-killer config set-openai-key 配置。"
                )
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        session = self._open_ready_session(url, allow_submit=allow_submit)
        try:
            try:
                executor = BrowserFormToolExecutor(session.page, allow_submit=allow_submit)
                runner = ResponsesToolLoop(
                    client=self.client,
                    model=self.model,
                    executor=executor,
                    max_steps=self.max_steps,
                )
                result = runner.run(prompt)
            except AgentBrowserFormError:
                raise
            except Exception as exc:
                raise AgentBrowserFormError(
                    "未命中预设 provider，已尝试 Agent Loop 浏览器回退，但浏览器操作失败："
                    f"{exc}"
                ) from exc
            self._save_session_state(session)
            return result
        finally:
            session.close()

    def _browser_snapshot(self, url: str) -> dict[str, Any]:
        session = self._open_ready_session(url, allow_submit=False)
        try:
            snapshot = BrowserFormToolExecutor(session.page, allow_submit=False).form_snapshot()
            self._save_session_state(session)
            return snapshot
        finally:
            session.close()

    def _fill_schema_values(
        self,
        url: str,
        schema: FormSchema,
        values: dict[str, Any],
        *,
        allow_submit: bool,
    ) -> dict[str, Any]:
        session = self._open_ready_session(url, allow_submit=allow_submit)
        try:
            try:
                executor = BrowserFormToolExecutor(session.page, allow_submit=allow_submit)
                snapshot = executor.form_snapshot()
                bindings = control_bindings_by_question_id(schema.raw)
                current_bindings = control_bindings_from_snapshot(snapshot)
                by_question = {question.id: question for question in schema.questions}
                filled: list[str] = []
                for question_id, value in values.items():
                    question = by_question.get(question_id)
                    binding = bindings.get(question_id)
                    if question is None or binding is None:
                        continue
                    current_binding = current_bindings.get(str(binding.get("handle") or ""))
                    if current_binding:
                        binding = {**binding, "handle": current_binding.get("handle") or binding.get("handle")}
                    binding = rebind_option_handles(binding, current_bindings)
                    question = question_for_current_binding(question, binding)
                    fill_schema_value(executor, question, binding, value)
                    filled.append(question_id)
                status: dict[str, Any] = {"ok": True, "filled": filled, "dry_run": not allow_submit}
                if allow_submit:
                    submit_handle = first_submit_button_handle(snapshot)
                    if not submit_handle:
                        raise AgentBrowserFormError("Agent Loop 浏览器回退未找到提交按钮。")
                    executor.form_click(submit_handle)
                    executor.form_wait(3000)
                    status["status"] = executor.form_read_status()
                self._save_session_state(session)
                return status
            except AgentBrowserFormError:
                raise
            except Exception as exc:
                raise AgentBrowserFormError(f"Agent Loop 浏览器回填失败：{exc}") from exc
        finally:
            session.close()

    def _save_session_state(self, session: Any) -> None:
        if not self.persist_session:
            return
        try:
            session.save_storage_state(self.session_path)
        except Exception:
            pass

    def _storage_path_for_open(self) -> Path | None:
        return self.session_path if self.persist_session else None

    def _open_ready_session(self, url: str, *, allow_submit: bool) -> AgentBrowserSession:
        session = self.browser_session_cls.open(storage_path=self._storage_path_for_open(), headless=self.headless)
        try:
            load_form_page(session.page, url)
            if page_looks_like_captcha(session.page):
                if not self.allow_handoff:
                    raise AgentBrowserFormError("检测到验证码页面，当前环境未启用人工验证交接，无法继续。")
                storage_path, cleanup_storage = self._save_handoff_storage_state(session)
                session.close()
                return self._run_captcha_handoff(url, storage_path=storage_path, cleanup_storage=cleanup_storage)
            return session
        except Exception:
            try:
                session.close()
            except Exception:
                pass
            raise

    def _save_handoff_storage_state(self, session: Any) -> tuple[Path | None, bool]:
        fallback_path = self._storage_path_for_open()
        if self.persist_session:
            try:
                session.save_storage_state(self.session_path)
            except Exception:
                pass
            return fallback_path, False
        storage_path = self.session_path.parent / f"agent-browser-handoff-{uuid4().hex}.json"
        try:
            session.save_storage_state(storage_path)
        except Exception:
            return fallback_path, False
        return storage_path, True

    def _run_captcha_handoff(self, url: str, *, storage_path: Path | None, cleanup_storage: bool) -> AgentBrowserSession:
        session = self.browser_session_cls.open(storage_path=storage_path, headless=False)
        if cleanup_storage and storage_path is not None:
            try:
                storage_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            attempts = 0
            while self.max_handoff_attempts is None or attempts < self.max_handoff_attempts:
                attempts += 1
                load_form_page(session.page, url)
                if not page_looks_like_captcha(session.page):
                    return session
                if self.handoff_waiter:
                    self.handoff_waiter()
                else:
                    input("请在打开的浏览器中完成验证码，然后回到终端按 Enter 继续...")
            load_form_page(session.page, url)
            if page_looks_like_captcha(session.page):
                raise AgentBrowserFormError("验证码仍未完成，无法继续解析表单。")
            return session
        except Exception:
            session.close()
            raise


class ResponsesToolLoop:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        executor: "BrowserFormToolExecutor",
        max_steps: int = DEFAULT_AGENT_BROWSER_MAX_STEPS,
    ) -> None:
        self.client = client
        self.model = model
        self.executor = executor
        self.max_steps = max_steps

    def run(self, prompt: str) -> dict[str, Any]:
        try:
            return self._run(prompt, use_previous_response_id=True)
        except BadRequestError as exc:
            if "previous_response_id" not in str(exc):
                raise
            return self._run(prompt, use_previous_response_id=False)

    def _run(self, prompt: str, *, use_previous_response_id: bool) -> dict[str, Any]:
        next_input: str | list[dict[str, Any]] = prompt
        previous_response_id: str | None = None
        stateless_input: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_status: dict[str, Any] = {}
        for _ in range(self.max_steps):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "input": next_input if use_previous_response_id else stateless_input,
                "tools": FORM_TOOL_DEFINITIONS,
                "parallel_tool_calls": False,
            }
            if use_previous_response_id and previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            response = self.client.responses.create(**kwargs)
            previous_response_id = str(getattr(response, "id", "") or previous_response_id or "")
            calls = list(extract_function_calls(response))
            if not calls:
                return parse_agent_json_response(response)

            outputs = []
            for call in calls:
                result = self.executor.call(call["name"], call["arguments"])
                last_status = result if isinstance(result, dict) else {"result": result}
                outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )
            next_input = outputs
            stateless_input = [{"role": "user", "content": prompt}, *function_calls_as_input(calls), *outputs]
        raise AgentBrowserFormError(f"Agent Loop 超过最大步数 {self.max_steps}，最后状态：{json.dumps(last_status, ensure_ascii=False)}")


class BrowserFormToolExecutor:
    def __init__(self, page: Any, *, allow_submit: bool) -> None:
        self.page = page
        self.allow_submit = allow_submit
        self.field_handles: set[str] = set()
        self.option_handles: dict[str, set[str]] = {}
        self.button_kinds: dict[str, str] = {}

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "form_snapshot":
            return self.form_snapshot()
        if name == "form_fill_field":
            return self.form_fill_field(str(arguments.get("handle") or ""), arguments.get("value"))
        if name == "form_choose_option":
            return self.form_choose_option(str(arguments.get("handle") or ""), str(arguments.get("option_handle") or ""))
        if name == "form_set_checkbox_group":
            option_handles = [str(value) for value in arguments.get("option_handles") or []]
            return self.form_set_checkbox_group(str(arguments.get("handle") or ""), option_handles)
        if name == "form_click":
            return self.form_click(str(arguments.get("handle") or ""))
        if name == "form_wait":
            return self.form_wait(int(arguments.get("milliseconds") or 1000))
        if name == "form_read_status":
            return self.form_read_status()
        return {"ok": False, "error": f"未知工具：{name}"}

    def form_snapshot(self) -> dict[str, Any]:
        snapshot = dict(self.page.evaluate(SNAPSHOT_SCRIPT))
        fields = list(snapshot.get("fields") or [])
        buttons = list(snapshot.get("buttons") or [])
        questions = list(snapshot.get("questions") or [])
        self.field_handles = {str(field.get("handle")) for field in fields if field.get("handle")}
        self.field_handles.update(str(question.get("handle")) for question in questions if question.get("handle"))
        self.option_handles = {
            str(field.get("handle")): {str(option.get("handle")) for option in field.get("options") or [] if option.get("handle")}
            for field in fields
            if field.get("handle")
        }
        all_field_handles = {str(field.get("handle")) for field in fields if field.get("handle")}
        for handle in list(self.option_handles):
            self.option_handles[handle].update(all_field_handles)
        for question in questions:
            handle = str(question.get("handle") or "")
            if handle:
                self.option_handles[handle] = {
                    str(option.get("handle")) for option in question.get("options") or [] if option.get("handle")
                }
        self.button_kinds = {
            str(button.get("handle")): str(button.get("kind") or "button")
            for button in buttons
            if button.get("handle")
        }
        return snapshot

    def form_fill_field(self, handle: str, value: Any) -> dict[str, Any]:
        self._require_field_handle(handle)
        return dict(self.page.evaluate(FILL_FIELD_SCRIPT, {"handle": handle, "value": value}))

    def form_choose_option(self, handle: str, option_handle: str) -> dict[str, Any]:
        self._require_option_handle(handle, option_handle)
        return dict(self.page.evaluate(CHOOSE_OPTION_SCRIPT, {"handle": handle, "option_handle": option_handle}))

    def form_set_checkbox_group(self, handle: str, option_handles: list[str]) -> dict[str, Any]:
        for option_handle in option_handles:
            self._require_option_handle(handle, option_handle)
        return dict(self.page.evaluate(SET_CHECKBOX_GROUP_SCRIPT, {"handle": handle, "option_handles": option_handles}))

    def form_click(self, handle: str) -> dict[str, Any]:
        if handle not in self.button_kinds:
            raise AgentBrowserFormError(f"工具只能点击 form_snapshot 返回的按钮 handle：{handle}")
        if self.button_kinds[handle] == "submit" and not self.allow_submit:
            raise AgentBrowserFormError("当前流程禁止点击提交按钮。")
        return dict(self.page.evaluate(CLICK_SCRIPT, {"handle": handle}))

    def form_wait(self, milliseconds: int) -> dict[str, Any]:
        bounded = max(100, min(milliseconds, 10_000))
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=bounded)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=bounded)
        except Exception:
            try:
                self.page.wait_for_timeout(bounded)
            except Exception:
                pass
        return {"ok": True, "waited_ms": bounded}

    def form_read_status(self) -> dict[str, Any]:
        return dict(self.page.evaluate(STATUS_SCRIPT))

    def _require_field_handle(self, handle: str) -> None:
        if handle not in self.field_handles:
            raise AgentBrowserFormError(f"工具只能使用 form_snapshot 返回的字段 handle：{handle}")

    def _require_option_handle(self, handle: str, option_handle: str) -> None:
        self._require_field_handle(handle)
        if option_handle not in self.option_handles.get(handle, set()):
            raise AgentBrowserFormError(f"工具只能使用 form_snapshot 返回的选项 handle：{option_handle}")


def extract_function_calls(response: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", []) or []:
        item_type = read_attr(item, "type")
        name = read_attr(item, "name")
        if item_type != "function_call" and not name:
            continue
        call_id = str(read_attr(item, "call_id") or read_attr(item, "id") or "")
        raw_arguments = read_attr(item, "arguments") or "{}"
        if isinstance(raw_arguments, str):
            arguments = json.loads(raw_arguments or "{}")
        else:
            arguments = dict(raw_arguments)
        calls.append({"name": str(name), "call_id": call_id, "arguments": arguments})
    return calls


def function_calls_as_input(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function_call",
            "call_id": call["call_id"],
            "name": call["name"],
            "arguments": json.dumps(call["arguments"], ensure_ascii=False),
        }
        for call in calls
    ]


def load_form_page(page: Any, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    for selector in (
        "input[name^='answer']",
        "textarea[name^='answer']",
        "form input, form textarea, form select",
        "input, textarea, select, [role='radio'], [role='checkbox']",
    ):
        try:
            page.wait_for_selector(selector, timeout=15_000)
            break
        except Exception:
            continue
    try:
        page.wait_for_load_state("networkidle", timeout=5_000)
    except Exception:
        pass


def page_looks_like_captcha(page: Any) -> bool:
    try:
        url = str(getattr(page, "url", "") or "").lower()
        title = str(page.title() or "").lower()
        text = str(page.locator("body").inner_text(timeout=1000) or "").lower()
    except Exception:
        return False
    markers = (
        "showcaptcha",
        "captcha",
        "smartcaptcha",
        "я не робот",
        "вы не робот",
        "i'm not a robot",
        "not a robot",
        "不是机器人",
    )
    haystack = "\n".join([url, title, text])
    return any(marker in haystack for marker in markers)
    try:
        page.wait_for_timeout(1_000)
    except Exception:
        pass


def parse_agent_json_response(response: Any) -> dict[str, Any]:
    output_text = str(getattr(response, "output_text", "") or extract_response_text(response)).strip()
    if not output_text:
        raise AgentBrowserFormError("Agent Loop 未返回可解析的 JSON 结果。")
    try:
        return json.loads(output_text)
    except json.JSONDecodeError as exc:
        raise AgentBrowserFormError(f"Agent Loop 返回内容不是 JSON：{output_text}") from exc


def extract_response_text(response: Any) -> str:
    chunks: list[str] = []
    for item in getattr(response, "output", []) or []:
        for content in read_attr(item, "content") or []:
            text = read_attr(content, "text")
            if text:
                chunks.append(str(text))
    return "".join(chunks)


def read_attr(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def parse_agent_browser_schema(payload: dict[str, Any], *, source_url: str) -> FormSchema:
    questions_payload = list(payload.get("questions") or [])
    questions: list[Question] = []
    metadata: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    for index, raw in enumerate(questions_payload, 1):
        question_id = str(raw.get("id") or raw.get("handle") or f"q_{index}")
        options = [
            Option(id=str(option.get("id") or option.get("handle") or option_index), label=str(option.get("label") or option.get("id") or option_index))
            for option_index, option in enumerate(raw.get("options") or [], 1)
        ]
        kind = normalize_browser_question_kind(str(raw.get("kind") or raw.get("raw_type") or "unsupported"), [option.model_dump() for option in options])
        if str(raw.get("kind") or "").lower() in {"file", "upload"}:
            kind = "unsupported"
        question = Question(
            id=question_id,
            label=str(raw.get("label") or question_id),
            kind=kind,
            raw_type=str(raw.get("raw_type") or raw.get("kind") or "unknown"),
            required=bool(raw.get("required", False)),
            options=options,
        )
        questions.append(question)
        binding = build_control_binding(
            question,
            handle=str(raw.get("handle") or question.id),
            options=raw.get("options") or [],
        )
        metadata.append(binding)
        bindings.append(binding)
    if not questions:
        raise AgentBrowserFormError("未命中预设 provider，已尝试 Agent Loop 浏览器回退，但未能识别到表单题目。")
    schema = FormSchema(
        id=str(payload.get("id") or extract_form_id(source_url, "agent-browser-form")),
        name=str(payload.get("name") or payload.get("title") or "Unknown Form"),
        submit_text=str(payload.get("submit_text") or "Submit"),
        questions=questions,
        raw={
            "provider": AGENT_BROWSER_PROVIDER,
            "document_type": "form",
            "source_url": source_url,
            AGENT_BROWSER_QUESTIONS_KEY: metadata,
            FORM_CONTROL_BINDINGS_KEY: bindings,
        },
    )
    try:
        return FormSchema.model_validate(schema)
    except ValidationError as exc:
        raise AgentBrowserFormError(f"Agent Loop 返回的表单结构无效：{exc}") from exc


def parse_agent_browser_snapshot(snapshot: dict[str, Any], *, source_url: str) -> FormSchema:
    questions = snapshot.get("questions") or snapshot.get("fields") or []
    return parse_agent_browser_schema(
        {
            "id": extract_form_id(source_url, "agent-browser-form"),
            "name": snapshot.get("title") or "Unknown Form",
            "submit_text": first_submit_button_label(snapshot) or "Submit",
            "questions": questions,
        },
        source_url=source_url,
    )


def first_submit_button_label(snapshot: dict[str, Any]) -> str | None:
    for button in snapshot.get("buttons") or []:
        if button.get("kind") == "submit" and button.get("label"):
            return str(button["label"])
    return None


def build_schema_prompt(url: str) -> str:
    return json.dumps(
        {
            "task": "Convert the current page DOM into the canonical intermediate FormSchema for an unknown form fallback. Use form_snapshot and return only JSON.",
            "url": url,
            "schema_contract": {
                "output": {"schema": {"id": "string", "name": "string", "submit_text": "string", "questions": "array"}},
                "question": {
                    "id": "stable string",
                    "handle": "one field handle from form_snapshot.fields",
                    "label": "human visible question text",
                    "kind": "string|text|integer|enum|checkbox|dropdown|boolean|unsupported",
                    "required": "boolean",
                    "options": [{"id": "stable option id", "handle": "option handle from form_snapshot", "label": "human visible option text"}],
                },
            },
            "rules": [
                "Do not solve captcha, bypass login, pay, or leave the current form workflow.",
                "Infer the schema from form_snapshot.dom_html and form_snapshot.visible_text.",
                "The fields list is only a DOM handle inventory; do not trust its text as a question label.",
                "Group controls into human-visible form questions when the DOM shows a question block.",
                "For input/select/textarea questions, use the handle of the fillable input/select/textarea as question.handle.",
                "For radio, checkbox, choice chips, ratings, and custom option widgets, use the handle of the visible option/control container as question.handle when there is no separate group handle.",
                "Every choice option must include option.handle copied exactly from the nearest clickable DOM node's data-form-killer-handle.",
                "For select/dropdown questions, use option handles from DOM option elements when present.",
                "For native select elements, include all visible option elements in options; prefer each option.value as option.id when available.",
                "Use only handles returned by form_snapshot.fields or present as data-form-killer-handle in dom_html for question.handle and option.handle.",
                "Keep option.id short and user-facing, such as the option value, 1-based visual index, or a concise slug. Do not use data-form-killer-handle as option.id unless no shorter stable id exists.",
                "If a visible question has no fillable control, include it only when it is a file/upload or unsupported field.",
                "Use stable question ids from DOM attributes when obvious; otherwise use handles or q_1, q_2 in visual order.",
                "Ignore decorative text, navigation, privacy notices, cookie banners, scripts, and validation helper text that is not part of a question label.",
                "File upload fields must be kind unsupported.",
                "Return JSON with key schema: {id, name, submit_text, questions}.",
                "Each question must include id, handle, label, kind, required, and options.",
                "Allowed kinds: string, text, integer, enum, checkbox, dropdown, boolean, unsupported.",
            ],
        },
        ensure_ascii=False,
    )


def build_submit_prompt(schema: FormSchema, values: dict[str, Any], *, dry_run: bool) -> str:
    metadata = schema.raw.get(AGENT_BROWSER_QUESTIONS_KEY) or []
    return json.dumps(
        {
            "task": "Fill the unknown web form using only the provided browser form tools.",
            "dry_run": dry_run,
            "rules": [
                "Call form_snapshot first and use only returned handles.",
                "Fill every provided value that has a matching question.",
                "For dry_run=true, do not click a submit button; return JSON {ok:true,dry_run:true}.",
                "For dry_run=false, click the submit button only after fields are filled, then read status.",
                "Return only JSON with ok, dry_run, and status/error when available.",
            ],
            "questions": metadata,
            "values": values,
        },
        ensure_ascii=False,
    )


def fill_schema_value(
    executor: BrowserFormToolExecutor,
    question: Question,
    binding: dict[str, Any],
    value: Any,
) -> None:
    handle = str(binding.get("handle") or question.id)
    if question.kind == "boolean" and binding.get("options"):
        text = str(value).strip().lower()
        wanted = "yes" if text in {"1", "true", "yes", "y", "是", "да"} else "no"
        option_handle = option_handle_for_value(binding, wanted) or option_handle_for_value(binding, value)
        if not option_handle:
            raise AgentBrowserFormError(f"未找到题目选项映射：{question.label} -> {value}")
        executor.form_choose_option(handle, option_handle)
        return
    if question.kind in {"string", "text", "integer", "boolean"}:
        executor.form_fill_field(handle, value)
        return
    if question.kind in {"enum", "dropdown"}:
        values = value if isinstance(value, list) else [value]
        if not values:
            return
        option_handle = option_handle_for_value(binding, values[0])
        if option_handle:
            executor.form_choose_option(handle, option_handle)
            return
        if question.kind == "enum":
            executor.form_choose_option(handle, str(values[0]))
            return
        raise AgentBrowserFormError(f"未找到题目选项映射：{question.label} -> {values[0]}")
        return
    if question.kind == "checkbox":
        values = value if isinstance(value, list) else [value]
        option_handles = []
        for item in values:
            option_handle = option_handle_for_value(binding, item)
            option_handles.append(option_handle or str(item))
        executor.form_set_checkbox_group(handle, option_handles)
        return
    raise AgentBrowserFormError(f"Agent Loop 浏览器回退暂不支持回填字段类型：{question.kind}")


def first_submit_button_handle(snapshot: dict[str, Any]) -> str | None:
    for button in snapshot.get("buttons") or []:
        if button.get("kind") == "submit" and button.get("handle"):
            return str(button["handle"])
    return None


def control_bindings_from_snapshot(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for raw in snapshot.get("fields") or []:
        fake_question = Question(
            id=str(raw.get("id") or raw.get("handle") or ""),
            label=str(raw.get("label") or raw.get("text") or raw.get("id") or raw.get("handle") or ""),
            kind=normalize_browser_question_kind(str(raw.get("kind") or "unsupported"), raw.get("options") or []),
            raw_type=str(raw.get("kind") or "unsupported"),
            options=[
                Option(id=str(option.get("id") or option.get("handle") or index), label=str(option.get("label") or option.get("id") or index))
                for index, option in enumerate(raw.get("options") or [], 1)
            ],
        )
        binding = build_control_binding(fake_question, handle=str(raw.get("handle") or fake_question.id), options=raw.get("options") or [])
        bindings[str(binding["handle"])] = binding
    return bindings


def question_for_current_binding(question: Question, binding: dict[str, Any]) -> Question:
    if question.options and binding.get("options"):
        return question
    if question.options and not binding.get("options"):
        return question.model_copy(update={"options": []})
    return question


def rebind_option_handles(binding: dict[str, Any], current_bindings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    options = list(binding.get("options") or [])
    if not options:
        return binding
    current_options = list((current_bindings.get(str(binding.get("handle") or "")) or {}).get("options") or [])
    if not current_options:
        for current in current_bindings.values():
            if str(current.get("id") or "") == str(binding.get("id") or ""):
                current_options = list(current.get("options") or [])
                break
    global_by_label = {str(current.get("label") or "").strip().lower(): str(current.get("handle") or "") for current in current_bindings.values()}
    global_by_id = {str(current.get("id") or "").strip().lower(): str(current.get("handle") or "") for current in current_bindings.values()}
    global_texts = [
        (str(current.get("label") or current.get("id") or "").strip().lower(), str(current.get("handle") or ""))
        for current in current_bindings.values()
        if current.get("handle")
    ]
    global_by_slug = {
        slug_handle_text(str(current.get("label") or current.get("id") or "")): str(current.get("handle") or "")
        for current in current_bindings.values()
    }
    by_label = {str(option.get("label") or "").strip().lower(): str(option.get("handle") or "") for option in current_options}
    by_id = {str(option.get("id") or "").strip().lower(): str(option.get("handle") or "") for option in current_options}
    rebound = []
    for option in options:
        label = str(option.get("label") or "").strip().lower()
        option_id = str(option.get("id") or "").strip().lower()
        handle = (
            by_label.get(label)
            or by_id.get(option_id)
            or global_by_label.get(label)
            or global_by_id.get(option_id)
            or global_by_slug.get(slug_handle_text(label))
            or global_by_slug.get(slug_handle_text(option_id))
            or closest_text_handle(label, global_texts)
            or closest_text_handle(option_id, global_texts)
            or str(option.get("handle") or "")
        )
        rebound.append({**option, "handle": handle})
    return {**binding, "options": rebound}


def slug_handle_text(value: str) -> str:
    return "".join("_" if not char.isalnum() else char.lower() for char in str(value)).strip("_")


def closest_text_handle(value: str, candidates: list[tuple[str, str]]) -> str | None:
    needle = str(value or "").strip().lower()
    if not needle:
        return None
    needle_slug = slug_handle_text(needle)
    for text, handle in candidates:
        if not text or not handle:
            continue
        text_slug = slug_handle_text(text)
        if needle in text or text in needle or (needle_slug and (needle_slug in text_slug or text_slug in needle_slug)):
            return handle
    if len(needle) <= 4:
        compact = needle.replace(" ", "")
        for text, handle in candidates:
            if compact and compact in text.replace(" ", ""):
                return handle
    return None


SNAPSHOT_SCRIPT = r"""
() => {
  const visible = (node) => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 2 && rect.height > 2 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity || "1") > 0.05;
  };
  const textOf = (node) => (node && (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim()) || "";
  const clean = (text) => String(text || "").replace(/\*/g, "").replace(/必填/g, "").replace(/\s+/g, " ").trim();
  const trimmed = (text, max = 400) => {
    const value = clean(text);
    return value.length > max ? value.slice(0, max) : value;
  };
  const safeHandlePart = (value) => String(value || "").replace(/[^a-zA-Z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 80);
  const ensureHandle = (node, prefix, index, stableValue = "") => {
    if (!node.dataset.formKillerHandle) {
      const stable = safeHandlePart(stableValue);
      const proposed = stable ? `${prefix}_${stable}` : `${prefix}_${index + 1}`;
      let handle = proposed;
      let suffix = 2;
      while (document.querySelector(`[data-form-killer-handle="${CSS.escape(handle)}"]`) && document.querySelector(`[data-form-killer-handle="${CSS.escape(handle)}"]`) !== node) {
        handle = `${proposed}_${suffix++}`;
      }
      node.dataset.formKillerHandle = handle;
    }
    return node.dataset.formKillerHandle;
  };
  const nativeControls = Array.from(document.querySelectorAll("input, textarea, select, option, button, [role='button'], [role='radio'], [role='checkbox'], [role='switch']"))
    .filter((node) => visible(node) && node.type !== "hidden");
  const clickableControls = Array.from(document.querySelectorAll("[aria-checked], [aria-selected], [class*='radio'], [class*='checkbox'], [class*='choice'], [class*='option']"))
    .filter((node) => visible(node) && textOf(node) && !nativeControls.some((control) => control === node || control.contains(node) || node.contains(control)));
  const controls = [...nativeControls, ...clickableControls];
  const fields = controls.map((control, index) => {
    const tag = (control.tagName || "").toLowerCase();
    const role = (control.getAttribute("role") || "").toLowerCase();
    const type = (control.getAttribute("type") || role || tag || "unknown").toLowerCase();
    const stableId = control.name || control.id || control.getAttribute("data-question-id") || control.getAttribute("data-qa") || control.getAttribute("data-testid") || "";
    const handle = ensureHandle(control, "field", index, stableId || textOf(control) || type);
    return {
      handle,
      id: stableId || handle,
      tag,
      type,
      role,
      name: control.name || "",
      value: control.getAttribute("value") || "",
      text: trimmed(control.getAttribute("aria-label") || control.getAttribute("placeholder") || textOf(control), 300),
      required: control.required === true || control.getAttribute("aria-required") === "true",
    };
  }).filter((field, index, all) => field.handle && all.findIndex((item) => item.handle === field.handle) === index);

  const buttons = Array.from(document.querySelectorAll("button, input[type='submit'], input[type='button'], [role='button']")).filter(visible).map((button, index) => {
    const label = clean(button.getAttribute("aria-label") || button.value || textOf(button));
    const kind = /提交|完成|submit|send/i.test(label) || button.type === "submit" ? "submit" : "next";
    return { handle: ensureHandle(button, "button", index, label || button.type), label, kind };
  }).filter((button) => button.label);
  const serializeNode = (node, depth = 0) => {
    if (!node || depth > 8 || !visible(node)) return "";
    const tag = (node.tagName || "").toLowerCase();
    if (["script", "style", "noscript", "svg", "path", "iframe"].includes(tag)) return "";
    const attrs = [];
    const handle = node.dataset && node.dataset.formKillerHandle;
    if (handle) attrs.push(`data-form-killer-handle="${handle}"`);
    for (const name of ["id", "name", "type", "role", "aria-label", "aria-required", "placeholder", "data-question-id", "data-qa", "data-testid", "data-test-id"]) {
      const value = node.getAttribute && node.getAttribute(name);
      if (value) attrs.push(`${name}="${String(value).replace(/"/g, "&quot;").slice(0, 120)}"`);
    }
    if (node.matches && node.matches("input, textarea, select, option, button")) {
      const value = node.matches("option") ? textOf(node) : node.getAttribute("value");
      if (value) attrs.push(`value="${String(value).replace(/"/g, "&quot;").slice(0, 120)}"`);
    }
    const children = Array.from(node.children || []).slice(0, 80).map((child) => serializeNode(child, depth + 1)).filter(Boolean);
    const directText = Array.from(node.childNodes || [])
      .filter((child) => child.nodeType === Node.TEXT_NODE)
      .map((child) => clean(child.textContent))
      .filter(Boolean)
      .join(" ");
    const body = [directText ? trimmed(directText, 200) : "", ...children].filter(Boolean).join("");
    if (!body && !handle && !attrs.length) return "";
    const open = `<${tag}${attrs.length ? " " + attrs.join(" ") : ""}>`;
    return `${open}${body}</${tag}>`;
  };
  const formRoots = Array.from(document.querySelectorAll("form, main, [role='main'], [class*='form'], [class*='survey'], body")).filter(visible);
  const root = formRoots.find((node) => node.querySelector("input, textarea, select, [role='radio'], [role='checkbox']")) || document.body;
  const dom_html = serializeNode(root).slice(0, 60000);
  const visible_text = trimmed(textOf(root), 20000);
  return { url: location.href, title: document.title || "", visible_text, dom_html, fields, buttons };
}
"""

FILL_FIELD_SCRIPT = r"""
({ handle, value }) => {
  const root = document.querySelector(`[data-form-killer-handle="${CSS.escape(handle)}"]`);
  if (!root) return { ok: false, error: `missing handle ${handle}` };
  const input = root.matches("input, textarea") ? root : root.querySelector("textarea, input:not([type='hidden'])");
  if (!input) return { ok: false, error: `missing input ${handle}` };
  input.focus();
  input.value = String(value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return { ok: true };
}
"""

CHOOSE_OPTION_SCRIPT = r"""
({ handle, option_handle }) => {
  const option = document.querySelector(`[data-form-killer-handle="${CSS.escape(option_handle)}"]`);
  if (!option) return { ok: false, error: `missing option ${option_handle}` };
  if (option.tagName === "OPTION") {
    const select = option.closest("select");
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true };
  }
  option.click();
  return { ok: true };
}
"""

SET_CHECKBOX_GROUP_SCRIPT = r"""
({ handle, option_handles }) => {
  const wanted = new Set(option_handles);
  const root = document.querySelector(`[data-form-killer-handle="${CSS.escape(handle)}"]`);
  if (!root) return { ok: false, error: `missing handle ${handle}` };
  const options = Array.from(root.querySelectorAll("input[type='checkbox'], [role='checkbox']"));
  for (const option of options) {
    const optionHandle = option.dataset.formKillerHandle;
    const checked = !!option.checked || option.getAttribute("aria-checked") === "true";
    const shouldCheck = wanted.has(optionHandle);
    if (checked !== shouldCheck) option.click();
  }
  return { ok: true };
}
"""

CLICK_SCRIPT = r"""
({ handle }) => {
  const button = document.querySelector(`[data-form-killer-handle="${CSS.escape(handle)}"]`);
  if (!button) return { ok: false, error: `missing button ${handle}` };
  button.click();
  return { ok: true };
}
"""

STATUS_SCRIPT = r"""
() => {
  const text = (document.body && (document.body.innerText || document.body.textContent || "").replace(/\s+/g, " ").trim()) || "";
  const success = /提交成功|已提交|success|thank you|thanks/i.test(text);
  const failure = /失败|错误|error|failed|required|必填/i.test(text);
  return { ok: true, url: location.href, title: document.title || "", success, failure, text: text.slice(0, 1000) };
}
"""
