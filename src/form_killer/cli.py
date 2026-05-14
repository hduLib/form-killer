from __future__ import annotations

import os
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from openai import AuthenticationError
from rich.console import Console
from rich.prompt import Confirm, Prompt

from form_killer.answers import (
    AnswerError,
    answer_from_mapping,
    build_values,
    llm_answers_to_payloads,
    load_answer_file,
    missing_required,
    question_needs_user,
)
from form_killer.config import (
    ApiKeySource,
    DEFAULT_MODEL,
    clear_api_key,
    ensure_config_file,
    load_api_key,
    load_api_key_with_source,
    load_base_url,
    load_codex_base_url,
    load_codex_model,
    load_model,
    save_api_key,
    save_base_url,
    save_model,
)
from form_killer.captcha import BrowserCaptchaSession, CaptchaHandoffError
from form_killer.events import ServiceEvent
from form_killer.forms.base import AnswerPayload, FormSchema, FormServiceError, Question
from form_killer.forms.agent_browser import AGENT_BROWSER_PROVIDER, AgentBrowserFormService
from form_killer.forms.services import (
    FormRouteError,
    RoutedFormService,
    form_providers,
    resolve_form_service as resolve_service_route,
    supported_provider_labels,
)
from form_killer.forms.yandex import YandexCaptchaError, YandexFormAdapter, YandexFormError, YandexFormService
from form_killer.llm.openai_answerer import OpenAIAnswerer
from form_killer.login import LoginTimeoutError, run_cli_login_events, run_cli_service_events
from form_killer.privacy import is_personal_question
from form_killer.ui import render_error, render_input_header, render_output_header, render_preview, render_schema, render_user_input_plan

SUPPORTED_URL_HELP = "已注册表单插件支持的 URL"

app = typer.Typer(
    help=f"中文 CLI 问卷答题工具。当前可用插件：{supported_provider_labels()}。",
    invoke_without_command=True,
)
config_app = typer.Typer(help="管理本地配置。")
app.add_typer(config_app, name="config")
console = Console()


def supported_url_prompt() -> str:
    labels = [provider.label for provider in form_providers()]
    if not labels:
        return "当前没有可用表单插件。请先安装或启用 provider 插件。"
    lines = ["当前可用表单插件：", *[f"- {label}" for label in labels], "", "请粘贴任一受支持的页面 URL，回车后开始解析。"]
    return "\n".join(lines)


@app.callback()
def main(ctx: typer.Context) -> None:
    """不带子命令运行时，直接输入表单 URL 开始答题。"""
    if ctx.invoked_subcommand is not None:
        return
    render_input_header(console, "表单地址", supported_url_prompt())
    url = Prompt.ask("[输入] 页面 URL").strip()
    if not url:
        render_error(console, "页面 URL 不能为空。")
        raise typer.Exit(1)
    answer(url=url, submit=True, interactive_setup=True)


@app.command("login")
def login_command(
    url: Annotated[str | None, typer.Argument(help="Tencent Docs/WPS Docs or form URL.")] = None,
) -> None:
    """Run the normal provider login flow and save the browser session."""
    url = url or Prompt.ask("请输入腾讯文档/WPS 文档或表单 URL").strip()
    if not url:
        render_error(console, "页面 URL 不能为空。")
        raise typer.Exit(1)

    try:
        routed = resolve_form_service(url)
        if not service_supports_login(routed.service):
            raise FormRouteError("login 命令当前仅支持需要浏览器登录的 provider。")

        label = service_label(routed)
        login_events = run_cli_login_events(console, provider_label=label)
        login_events(ServiceEvent("login_required", provider=routed.provider, provider_label=label))
        try:
            login_name = routed.service.login(url, event_handler=login_events)
        finally:
            login_events.close()
        if login_name:
            console.print(f"[green]{label}登录成功：{login_name}，会话已保存。[/green]")
        else:
            console.print(f"[green]{label}登录成功，会话已保存。[/green]")
    except (FormRouteError, FormServiceError, LoginTimeoutError) as exc:
        render_error(console, str(exc))
        raise typer.Exit(1) from exc


@app.command()
def inspect(
    url: Annotated[str | None, typer.Argument(help=SUPPORTED_URL_HELP)] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="Agent Loop 回退使用的 OpenAI API Key。")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Agent Loop 回退使用的 OpenAI 兼容接口 Base URL。")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Agent Loop 回退使用的 OpenAI 模型。")] = None,
    agent: Annotated[bool, typer.Option("--agent", help="强制使用 Agent Loop 浏览器工具路线，跳过预设 provider。")] = False,
    debug_browser: Annotated[bool, typer.Option("--debug-browser", help="遇到站点验证时打开浏览器供手动检查。")] = False,
) -> None:
    """解析并显示表单结构，不调用 AI，不提交。"""
    url = url or Prompt.ask("请输入页面 URL").strip()
    try:
        routed = resolve_cli_form_service(url, force_agent=agent)
        if is_agent_browser_route(routed):
            configure_agent_browser_service(routed.service, api_key, base_url, model)
            configure_agent_browser_handoff(routed.service, allow_handoff=True)
        schema = fetch_schema_with_handoff(routed, url, allow_handoff=debug_browser)
    except (FormRouteError, FormServiceError, LoginTimeoutError) as exc:
        if isinstance(exc, YandexCaptchaError):
            render_error(console, f"{exc}\n表单链接：{exc.url}")
        else:
            maybe_open_debug_browser(url, debug_browser, exc)
            render_error(console, str(exc))
        raise typer.Exit(1) from exc
    render_schema(console, schema)


@app.command()
def answer(
    url: Annotated[str | None, typer.Argument(help=f"{SUPPORTED_URL_HELP}；不填则进入交互输入。")] = None,
    answers: Annotated[Path | None, typer.Option("--answers", "-a", help="个人信息/文件等用户答案 JSON。")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="OpenAI API Key，优先级高于配置和环境变量。")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="OpenAI 兼容接口 Base URL，优先级高于配置和环境变量。")] = None,
    model: Annotated[str | None, typer.Option("--model", help="使用的 OpenAI 模型，优先级高于配置和环境变量。")] = None,
    submit: Annotated[bool, typer.Option("--submit", help="预览并确认后提交到目标平台。")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="非交互确认；只在同时传入 --submit 时有效。")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="只做 dry-run 校验，不最终提交。")] = False,
    debug_browser: Annotated[bool, typer.Option("--debug-browser", help="预留给浏览器调试；默认纯 CLI。")] = False,
    agent: Annotated[bool, typer.Option("--agent", help="强制使用 Agent Loop 浏览器工具路线，跳过预设 provider。")] = False,
    interactive_setup: Annotated[bool, typer.Option(hidden=True)] = False,
) -> None:
    """开始答题：解析表单、让 AI 作答、预览结果、可选提交。"""
    if not url:
        render_input_header(console, "表单地址", supported_url_prompt())
    url = url or Prompt.ask("[输入] 页面 URL").strip()
    if not url:
        render_error(console, "页面 URL 不能为空。")
        raise typer.Exit(1)
    if debug_browser:
        console.print("[yellow]当前主流程仍使用纯 CLI；debug-browser 仅作为后续排查入口。[/yellow]")

    try:
        routed = resolve_cli_form_service(url, force_agent=agent)
        service = routed.service
        key: str | None = None
        resolved_base_url: str | None = None
        resolved_model = DEFAULT_MODEL
        key_source: ApiKeySource = "missing"
        if is_agent_browser_route(routed):
            key, resolved_base_url, resolved_model, key_source = resolve_openai_settings(api_key, base_url, model)
            configure_agent_browser_service(service, key, resolved_base_url, resolved_model, already_resolved=True)
            configure_agent_browser_handoff(service, allow_handoff=True)
        schema = fetch_schema_with_handoff(
            routed,
            url,
            allow_handoff=interactive_setup or debug_browser,
        )
        render_output_header(console, "表单解析完成", "已从页面 URL 解析出题目、字段类型、必填状态和选项。")
        render_schema(console, schema)
        if key is None:
            key, resolved_base_url, resolved_model, key_source = resolve_openai_settings(api_key, base_url, model)
        mapping = load_answer_file(answers)
        initial_llm_questions = select_llm_questions(schema, mapping)
        if interactive_setup and initial_llm_questions:
            key, resolved_base_url, resolved_model, key_source = prompt_for_openai_settings(
                key, resolved_base_url, resolved_model, key_source
            )
        answerer: OpenAIAnswerer | None = None
        if initial_llm_questions:
            if not key:
                raise AnswerError("缺少 OpenAI API Key。请使用 OPENAI_API_KEY、--api-key，或 form-killer config set-openai-key 配置。")
            answerer = OpenAIAnswerer(api_key=key, model=resolved_model, base_url=resolved_base_url)

        ai_personal_question_ids: set[str] = set()
        if answerer and initial_llm_questions:
            try:
                with console.status("[bold magenta]AI 正在判断哪些字段需要用户填写...[/bold magenta]", spinner="dots"):
                    ai_personal_question_ids = answerer.classify_personal_questions(schema, initial_llm_questions)
            except AuthenticationError as exc:
                raise invalid_api_key_error(key_source) from exc

        user_question_ids = {
            question.id
            for question in schema.questions
            if answer_from_mapping(question, mapping) is None
            and (question_needs_user(question) or question.id in ai_personal_question_ids)
        }
        render_input_header(console, "用户补充", "以下字段不会交给 AI，需要你在 CLI 中手动输入。")
        render_user_input_plan(console, schema, user_question_ids)
        collected = collect_user_answers(schema, mapping, user_question_ids)
        llm_questions = select_llm_questions(
            schema,
            mapping,
            answered_ids={answer.question_id for answer in collected},
            personal_question_ids=ai_personal_question_ids,
            exclude_local_personal=True,
        )
        llm_payloads: list[AnswerPayload] = []
        if llm_questions:
            if not key:
                raise AnswerError("缺少 OpenAI API Key。请使用 OPENAI_API_KEY、--api-key，或 form-killer config set-openai-key 配置。")
            try:
                answerer = answerer or OpenAIAnswerer(api_key=key, model=resolved_model, base_url=resolved_base_url)
                with console.status("[bold magenta]AI 正在思考并生成格式化答案...[/bold magenta]", spinner="dots"):
                    llm_payloads = llm_answers_to_payloads(answerer.answer(schema, llm_questions), schema)
            except AuthenticationError as exc:
                raise invalid_api_key_error(key_source) from exc

        all_answers = merge_answers(schema, collected + llm_payloads)
        values = prepare_values(service, schema, all_answers)
        missing = missing_required(schema, values)
        render_output_header(console, "答案生成完成", "以下是准备回填到表单的答案，请核对后再决定是否提交。")
        render_preview(console, schema, all_answers)

        if missing:
            labels = "\n".join(f"- {question.label} ({question.id})" for question in missing)
            raise AnswerError(f"以下必填项缺失，无法提交：\n{labels}")

        if dry_run:
            submit_events = run_cli_service_events(console, provider_label=str(getattr(service, "label", routed.provider)))
            try:
                service_submit(service, schema, values, dry_run=True, event_handler=submit_events)
            finally:
                submit_events.close()
            console.print("[green]dry-run 校验通过，没有发送最终提交。[/green]")
            return

        if not submit:
            console.print("[yellow]答案预览已完成，尚未提交。如需提交，请重新运行并添加 --submit。[/yellow]")
            return

        if not yes and not Confirm.ask("确认提交到目标平台？", default=False):
            console.print("[yellow]已取消提交，没有发送最终提交请求。[/yellow]")
            return

        submit_events = run_cli_service_events(console, provider_label=str(getattr(service, "label", routed.provider)))
        try:
            response = service_submit(service, schema, values, dry_run=False, event_handler=submit_events)
        finally:
            submit_events.close()
        console.print("[green]提交成功。[/green]")
        if response.get("answer_key"):
            answer_key = str(response["answer_key"])
            console.print(f"Answer key: [bold]{answer_key}[/bold]")
            verify_submission(service, schema, answer_key)
    except (AnswerError, FormRouteError, FormServiceError, LoginTimeoutError, ValueError) as exc:
        if isinstance(exc, YandexCaptchaError):
            render_error(console, f"{exc}\n表单链接：{exc.url}")
        elif isinstance(exc, YandexFormError):
            maybe_open_debug_browser(url, debug_browser, exc)
            render_error(console, str(exc))
        else:
            render_error(console, str(exc))
        raise typer.Exit(1) from exc


@config_app.command("set-openai-key")
def set_openai_key(
    api_key: Annotated[str | None, typer.Option("--api-key", help="OpenAI API key. Hidden prompt if omitted.")] = None,
    allow_plaintext: Annotated[
        bool,
        typer.Option("--allow-plaintext", help="兼容旧参数；API Key 始终保存到项目 .form-killer/config.toml。"),
    ] = False,
) -> None:
    """保存 OpenAI API Key 到项目本地配置。"""
    value = api_key or Prompt.ask("OpenAI API Key", password=True)
    target = save_api_key(value, allow_plaintext=allow_plaintext)
    console.print(f"[green]OpenAI API Key 已保存到 {target}。[/green]")


@config_app.command("set-openai-base-url")
def set_openai_base_url(
    base_url: Annotated[str | None, typer.Argument(help="OpenAI 兼容接口 Base URL；不填则交互输入。")] = None,
) -> None:
    """保存 OpenAI 兼容接口 Base URL。"""
    value = (base_url or Prompt.ask("OpenAI Base URL，例如 https://api.openai.com/v1")).strip()
    if not value:
        render_error(console, "Base URL 不能为空。")
        raise typer.Exit(1)
    target = save_base_url(value)
    console.print(f"[green]OpenAI Base URL 已保存到 {target}。[/green]")


@config_app.command("set-openai-model")
def set_openai_model(
    model: Annotated[str | None, typer.Argument(help="OpenAI 模型名；不填则交互输入。")] = None,
) -> None:
    """保存默认 OpenAI 模型。"""
    value = (model or Prompt.ask("OpenAI 模型", default=DEFAULT_MODEL)).strip()
    if not value:
        render_error(console, "模型名不能为空。")
        raise typer.Exit(1)
    target = save_model(value)
    console.print(f"[green]OpenAI 模型已保存到 {target}：{value}[/green]")


@config_app.command("get")
def get_config() -> None:
    """查看 OpenAI 配置，不打印密钥内容。"""
    config_path = ensure_config_file()
    key = load_api_key()
    base_url = load_base_url()
    model = load_model()
    console.print(f"[green]配置文件: {config_path}[/green]")
    console.print("[green]已配置 OpenAI API Key。[/green]" if key else "[yellow]尚未配置 OpenAI API Key。[/yellow]")
    if base_url:
        console.print(f"[green]OpenAI Base URL: {base_url}[/green]")
    else:
        console.print("[yellow]尚未配置 OpenAI Base URL，将使用 SDK 默认地址。[/yellow]")
    console.print(f"[green]OpenAI 模型: {model or DEFAULT_MODEL}[/green]")


@config_app.command("path")
def config_path() -> None:
    """显示可手动编辑的配置文件路径，并在缺失时创建带注释模板。"""
    path = ensure_config_file()
    console.print(f"[green]{path}[/green]")


@config_app.command("clear")
def clear_config() -> None:
    """清除保存的 OpenAI 配置。"""
    cleared = clear_api_key()
    if cleared:
        console.print("[green]已清除: " + ", ".join(cleared) + "[/green]")
    else:
        console.print("[yellow]没有找到已保存的 API Key。[/yellow]")


def collect_user_answers(
    schema: FormSchema, mapping: dict[str, object], user_question_ids: set[str] | None = None
) -> list[AnswerPayload]:
    answers: list[AnswerPayload] = []
    for question in schema.questions:
        mapped = answer_from_mapping(question, mapping)
        if mapped:
            answers.append(mapped)
            continue
        if question_needs_user(question) or (user_question_ids is not None and question.id in user_question_ids):
            answers.append(prompt_for_question(question))
    return answers


def resolve_form_service(url: str, *, force_agent: bool = False) -> RoutedFormService:
    if force_agent:
        return RoutedFormService(provider=AGENT_BROWSER_PROVIDER, document_type="form", service=AgentBrowserFormService())
    try:
        routed = resolve_service_route(url, allow_agent_browser_fallback=False)
    except FormRouteError:
        if getattr(YandexFormAdapter, "__module__", "") != "form_killer.forms.yandex":
            return RoutedFormService(provider="yandex", document_type="form", service=YandexFormService(YandexFormAdapter()))
        routed = resolve_service_route(url)
    if routed.provider == "yandex":
        return RoutedFormService(provider="yandex", document_type="form", service=YandexFormService(YandexFormAdapter()))
    return routed


def resolve_cli_form_service(url: str, *, force_agent: bool = False) -> RoutedFormService:
    try:
        return resolve_form_service(url, force_agent=force_agent)
    except TypeError as exc:
        if "force_agent" not in str(exc):
            raise
        if force_agent:
            return RoutedFormService(provider=AGENT_BROWSER_PROVIDER, document_type="form", service=AgentBrowserFormService())
        return resolve_form_service(url)


def is_agent_browser_route(routed: RoutedFormService) -> bool:
    return routed.provider == AGENT_BROWSER_PROVIDER or isinstance(routed.service, AgentBrowserFormService)


def configure_agent_browser_service(
    service,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    *,
    already_resolved: bool = False,
) -> None:
    if already_resolved:
        key, resolved_base_url, resolved_model = api_key, base_url, model or DEFAULT_MODEL
    else:
        key, resolved_base_url, resolved_model, _key_source = resolve_openai_settings(api_key, base_url, model)
    if hasattr(service, "configure_openai"):
        service.configure_openai(api_key=key, base_url=resolved_base_url, model=resolved_model)


def configure_agent_browser_handoff(service, *, allow_handoff: bool) -> None:
    if hasattr(service, "configure_handoff"):
        service.configure_handoff(
            allow_handoff=allow_handoff,
            handoff_waiter=lambda: Prompt.ask("[输入] 在浏览器中完成验证码后，回到这里按 Enter 继续", default="", show_default=False),
        )


def provider_label(routed: RoutedFormService) -> str:
    return service_label(routed)


def service_label(routed: RoutedFormService) -> str:
    fallback = {
        "tencent": "腾讯文档",
        "wps": "WPS/金山表单",
        "yandex": "Yandex Forms",
    }.get(routed.provider, routed.provider)
    return str(getattr(routed.service, "label", None) or fallback)


def service_supports_login(service) -> bool:
    configured = getattr(service, "supports_login", None)
    if configured is not None:
        return bool(configured)
    return callable(getattr(service, "login", None))


def login_required_error_type(routed: RoutedFormService) -> type[Exception] | None:
    configured = getattr(routed.service, "login_required_error", None)
    if isinstance(configured, type) and issubclass(configured, Exception):
        return configured
    if routed.provider in {"tencent", "wps"}:
        return FormServiceError
    return None


def fetch_schema_with_handoff(
    routed: RoutedFormService,
    url: str,
    *,
    allow_handoff: bool,
) -> FormSchema:
    login_error = login_required_error_type(routed)
    if login_error is None:
        return fetch_schema_with_captcha_handoff(routed, url, allow_handoff=True)
    return fetch_browser_form_schema_with_login_handoff(
        routed,
        url,
        login_error_type=login_error,
        allow_handoff=allow_handoff,
    )


def fetch_schema_with_captcha_handoff(
    routed: RoutedFormService,
    url: str,
    *,
    allow_handoff: bool,
) -> FormSchema:
    service = getattr(routed, "service", routed)
    provider = str(getattr(routed, "provider", "yandex"))
    label = service_label(routed) if hasattr(routed, "service") else str(getattr(service, "label", "Yandex Forms"))
    events = run_cli_service_events(console, provider_label=label)
    try:
        return service_fetch_schema(service, url, event_handler=events)
    except YandexCaptchaError:
        if not allow_handoff:
            raise
    finally:
        events.close()

    render_input_header(
        console,
        "Yandex 验证",
        "检测到 SmartCaptcha。工具会打开一个带有当前会话 cookies 的浏览器窗口；请你手动完成验证。",
    )
    try:
        browser_session = BrowserCaptchaSession.open(url, service.export_browser_cookies())
    except CaptchaHandoffError as exc:
        raise YandexCaptchaError(url) from exc

    try:
        Prompt.ask("[输入] 在浏览器中完成验证后，回到这里按 Enter 继续", default="", show_default=False)
        service.import_browser_cookies(browser_session.cookies())
    finally:
        browser_session.close()

    events = run_cli_service_events(console, provider_label=label)
    try:
        events(ServiceEvent("parse_retry_start", provider=provider, provider_label=label))
        return service_fetch_schema(service, url, event_handler=events)
    finally:
        events.close()


def fetch_browser_form_schema_with_login_handoff(
    routed: RoutedFormService,
    url: str,
    *,
    login_error_type: type[Exception],
    allow_handoff: bool,
) -> FormSchema:
    service = routed.service
    label = service_label(routed)
    events = run_cli_service_events(console, provider_label=label)
    try:
        return service_fetch_schema(service, url, event_handler=events)
    except login_error_type:
        if not allow_handoff:
            raise
    finally:
        events.close()

    login_events = run_cli_service_events(console, provider_label=label)
    login_events(ServiceEvent("login_required", provider=routed.provider, provider_label=label))
    try:
        service.login(url, event_handler=login_events)
    finally:
        login_events.close()
    retry_events = run_cli_service_events(console, provider_label=label)
    try:
        retry_events(ServiceEvent("parse_retry_start", provider=routed.provider, provider_label=label))
        return service_fetch_schema(service, url, event_handler=retry_events)
    finally:
        retry_events.close()


def prompt_for_question(question: Question) -> AnswerPayload:
    if question.kind == "file":
        prompt = f"[输入] {question.label}（文件路径）"
    else:
        prompt = f"[输入] {question.label}"
    value = Prompt.ask(prompt)
    return AnswerPayload(
        question_id=question.id,
        value=value,
        source="file" if question.kind == "file" else "user",
    )


def merge_answers(schema: FormSchema, answers: list[AnswerPayload]) -> list[AnswerPayload]:
    by_id: dict[str, AnswerPayload] = {}
    for answer in answers:
        if answer.needs_user_input:
            continue
        by_id[answer.question_id] = answer
    return [by_id[question.id] for question in schema.questions if question.id in by_id]


def prepare_values(adapter, schema: FormSchema, answers: list[AnswerPayload]) -> dict[str, object]:
    by_question = {question.id: question for question in schema.questions}
    prepared: list[AnswerPayload] = []
    for answer_payload in answers:
        question = by_question[answer_payload.question_id]
        if question.kind == "file":
            upload_result = adapter.upload_file(schema, question, Path(str(answer_payload.value)))
            prepared.append(answer_payload.model_copy(update={"value": [upload_result], "source": "file"}))
        else:
            prepared.append(answer_payload)
    return build_values(schema, prepared)


def service_submit(service, schema: FormSchema, values: dict[str, object], *, dry_run: bool, event_handler):
    try:
        return service.submit(schema, values, dry_run=dry_run, event_handler=event_handler)
    except TypeError as exc:
        if "event_handler" not in str(exc):
            raise
        return service.submit(schema, values, dry_run=dry_run)


def service_fetch_schema(service, url: str, *, event_handler):
    try:
        return service.fetch_schema(url, event_handler=event_handler)
    except TypeError as exc:
        if "event_handler" not in str(exc):
            raise
        return service.fetch_schema(url)


def select_llm_questions(
    schema: FormSchema,
    mapping: dict[str, object],
    *,
    answered_ids: set[str] | None = None,
    personal_question_ids: set[str] | None = None,
    exclude_local_personal: bool = False,
) -> list[Question]:
    answered_ids = answered_ids or set()
    personal_question_ids = personal_question_ids or set()
    return [
        question
        for question in schema.questions
        if question.id not in answered_ids
        and answer_from_mapping(question, mapping) is None
        and not question_needs_user(question)
        and (not exclude_local_personal or not is_personal_question(question))
        and question.id not in personal_question_ids
        and question.kind != "unsupported"
    ]


def resolve_openai_settings(
    api_key: str | None,
    base_url: str | None,
    model: str | None,
) -> tuple[str | None, str | None, str, ApiKeySource]:
    key, key_source = resolve_openai_api_key(api_key)
    resolved_base_url = base_url or os.getenv("OPENAI_BASE_URL") or load_base_url() or load_codex_base_url()
    resolved_model = model or os.getenv("OPENAI_MODEL") or load_model() or load_codex_model() or DEFAULT_MODEL
    return key, resolved_base_url, resolved_model, key_source


def resolve_openai_api_key(api_key: str | None) -> tuple[str | None, ApiKeySource]:
    if api_key:
        return api_key, "cli"
    env_key = os.getenv("OPENAI_API_KEY")
    if env_key:
        return env_key, "env"
    return load_api_key_with_source()


def invalid_api_key_error(key_source: ApiKeySource) -> AnswerError:
    if key_source == "config":
        cleared = clear_api_key()
        detail = "，已删除保存的 API Key，请重新配置。" if cleared else "，但没有找到可删除的已保存 API Key。"
        return AnswerError(f"OpenAI API Key 无效{detail}")
    return AnswerError("OpenAI API Key 无效。当前 key 来自命令行或环境变量，请检查后重试。")


def prompt_for_openai_settings(
    key: str | None, base_url: str | None, model: str, key_source: ApiKeySource
) -> tuple[str | None, str | None, str, ApiKeySource]:
    if not key:
        render_input_header(console, "OpenAI API Key", "检测到当前没有可用 API Key，请输入后继续。")
        key = Prompt.ask("[输入] OpenAI API Key", password=True).strip()
        key_source = "cli"
        if key and Confirm.ask("[输入] 是否保存 API Key 供下次使用？", default=True):
            target = save_api_key(key)
            key_source = "config"
            console.print(f"[green]OpenAI API Key 已保存到 {target}。[/green]")
    if base_url is None:
        render_input_header(console, "OpenAI Base URL", "如使用 OpenAI 官方接口可直接回车；兼容接口请填 Base URL。")
        raw_base_url = Prompt.ask("[输入] OpenAI Base URL", default="", show_default=False).strip()
        base_url = raw_base_url or None
        if base_url and Confirm.ask("[输入] 是否保存 Base URL 供下次使用？", default=True):
            target = save_base_url(base_url)
            console.print(f"[green]OpenAI Base URL 已保存到 {target}。[/green]")
    render_input_header(console, "模型选择", "确认本次用于答题的模型；直接回车使用当前默认值。")
    chosen_model = Prompt.ask("[输入] OpenAI 模型", default=model).strip()
    model = chosen_model or model
    if model != load_model() and Confirm.ask("[输入] 是否保存该模型为默认模型？", default=False):
        target = save_model(model)
        console.print(f"[green]OpenAI 模型已保存到 {target}：{model}[/green]")
    return key, base_url, model, key_source


def maybe_open_debug_browser(url: str, debug_browser: bool, exc: Exception) -> None:
    if not debug_browser or "SmartCaptcha" not in str(exc):
        return
    opened = webbrowser.open(url)
    if opened:
        console.print("[yellow]已打开浏览器。请手动完成 Yandex 验证；CLI 不会代解 CAPTCHA。[/yellow]")
    else:
        console.print(f"[yellow]未能自动打开浏览器，请手动复制下面的链接到浏览器完成验证：\n{url}[/yellow]")


def verify_submission(adapter, schema: FormSchema, answer_key: str) -> None:
    try:
        if hasattr(adapter, "verify_submission"):
            success = adapter.verify_submission(schema, answer_key)
        else:
            success = adapter.get_success(schema, answer_key)
    except YandexFormError as exc:
        console.print(f"[yellow]提交已返回 answer_key，但二次验证失败：{exc}[/yellow]")
        return

    if success.get("answer_key") == answer_key and success.get("answer_id"):
        console.print(
            "[green]已通过 Yandex 成功页接口验证提交。"
            f" answer_id: {success['answer_id']}[/green]"
        )
    else:
        console.print("[yellow]Yandex 返回了成功页数据，但未匹配到当前 answer_key。[/yellow]")


if __name__ == "__main__":
    app()
