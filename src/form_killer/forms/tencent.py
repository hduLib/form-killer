from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from form_killer.config import load_tencent_headless, load_tencent_login_timeout_seconds, load_tencent_session_path
from form_killer.events import ServiceEvent, ServiceEventHandler
from form_killer.forms.base import FormLoginRequired, FormSchema, FormServiceError, Question
from form_killer.forms.browser_common import (
    BrowserFormSession,
    build_browser_form_schema,
    extract_form_id,
    extract_questions_from_page as extract_browser_questions_from_page,
    has_auth_cookie,
    has_authenticated_signal,
    has_form_controls,
    login_challenge_image_bytes,
    normalize_browser_question_kind,
    page_needs_login as browser_page_needs_login,
    read_login_name_from_account_pages,
    read_login_name_from_page as read_browser_login_name_from_page,
    run_browser_login,
    safe_page_title as browser_safe_page_title,
    wait_for_form_ready,
)
from form_killer.forms.services import HostFormProvider, register_form_provider


TencentDocumentType = Literal["form", "unsupported"]
TENCENT_FORM_CONTROL_SELECTOR = ".question-main, textarea, [role='radio'], [role='checkbox']"
TENCENT_AUTH_COOKIE_NAMES = {"uid_key", "tok", "doc_sid", "loginuin"}
TENCENT_ACCOUNT_URLS = ("https://docs.qq.com/desktop/", "https://docs.qq.com/")


class TencentFormError(FormServiceError):
    pass


class TencentUnsupportedDocumentError(TencentFormError):
    pass


class TencentLoginRequired(FormLoginRequired, TencentFormError):
    def __init__(self, url: str) -> None:
        super().__init__(url, "腾讯文档需要登录。工具会在后台获取二维码/验证码并输出到 CLI，请你完成登录后继续。")


@dataclass(frozen=True)
class TencentRawQuestion:
    id: str
    label: str
    kind: str
    required: bool
    options: list[dict[str, str]]
    dom_index: int
    selector: str | None = None


class TencentBrowserSession(BrowserFormSession):
    @classmethod
    def open(cls, *, storage_path: Path, headless: bool) -> "TencentBrowserSession":
        return cls.open_for_provider(
            storage_path=storage_path,
            headless=headless,
            error_type=TencentFormError,
            missing_playwright_message="缺少 Playwright，无法打开腾讯文档登录浏览器。",
        )


class TencentFormService:
    provider = "tencent"
    label = "腾讯文档"
    supports_login = True
    login_required_error = TencentLoginRequired

    def __init__(
        self,
        *,
        document_type: TencentDocumentType = "form",
        session_path: Path | None = None,
        login_timeout_seconds: int | None = None,
        headless: bool | None = None,
        browser_session_cls: type[TencentBrowserSession] = TencentBrowserSession,
    ) -> None:
        self.document_type = document_type
        self.session_path = session_path or load_tencent_session_path()
        self.login_timeout_seconds = login_timeout_seconds or load_tencent_login_timeout_seconds()
        self.headless = load_tencent_headless() if headless is None else headless
        self.browser_session_cls = browser_session_cls

    def fetch_schema(self, url: str, event_handler: ServiceEventHandler | None = None) -> FormSchema:
        if self.document_type != "form":
            raise TencentUnsupportedDocumentError("暂不支持该腾讯文档类型。当前仅支持腾讯文档收集表/表单。")

        if event_handler:
            event_handler(ServiceEvent("parse_start", provider=self.provider, provider_label=self.label))
        session = self.browser_session_cls.open(storage_path=self.session_path, headless=self.headless)
        try:
            session.page.goto(url, wait_until="networkidle", timeout=60_000)
            wait_for_tencent_form_ready(session.page)
            title = safe_page_title(session.page) or "Tencent Form"
            raw_questions = extract_questions_from_page(session.page)
            if not raw_questions and page_needs_login(session.page):
                raise TencentLoginRequired(url)
            if not raw_questions:
                raise TencentFormError("未能从腾讯文档页面识别到表单题目。请确认这是收集表/表单链接。")
            session.save_storage_state(self.session_path)
            return parse_tencent_questions(raw_questions, source_url=url, title=title)
        finally:
            session.close()

    def login(
        self,
        url: str,
        wait_for_user: Callable[[], None] | None = None,
        login_runner: Callable[[Any, Callable[[], bool], int], None] | None = None,
        event_handler: ServiceEventHandler | None = None,
    ) -> str | None:
        return run_browser_login(
            browser_session_cls=self.browser_session_cls,
            session_path=self.session_path,
            url=url,
            login_timeout_seconds=self.login_timeout_seconds,
            is_login_complete=is_login_complete,
            get_login_name=get_tencent_login_name,
            login_required_type=TencentLoginRequired,
            timeout_message="腾讯文档 login timed out. Please run login again.",
            challenge_image_bytes=login_challenge_image_bytes,
            wait_for_user=wait_for_user,
            login_runner=login_runner,
            event_handler=event_handler,
        )

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        raise TencentFormError("腾讯文档表单 v1 暂不支持文件上传字段。")

    def submit(
        self,
        schema: FormSchema,
        values: dict[str, Any],
        *,
        dry_run: bool,
        event_handler: ServiceEventHandler | None = None,
    ) -> dict[str, Any]:
        if dry_run:
            if event_handler:
                event_handler(ServiceEvent("dry_run_start", provider=self.provider, provider_label=self.label))
            return {"ok": True, "dryRun": True}

        source_url = str(schema.raw.get("source_url") or "")
        if not source_url:
            raise TencentFormError("缺少腾讯文档来源 URL，无法提交。")

        if event_handler:
            event_handler(ServiceEvent("fill_start", provider=self.provider, provider_label=self.label))
        session = self.browser_session_cls.open(storage_path=self.session_path, headless=self.headless)
        try:
            session.page.goto(source_url, wait_until="networkidle", timeout=60_000)
            wait_for_tencent_form_ready(session.page)
            if page_needs_login(session.page):
                raise TencentLoginRequired(source_url)
            fill_result = session.page.evaluate(
                FILL_SCRIPT,
                {"values": values, "questions": schema.raw.get("tencent_questions") or []},
            )
            if not fill_result.get("ok"):
                raise TencentFormError(str(fill_result.get("error") or "腾讯文档表单填写失败。"))
            clicked = click_submit_button(session.page)
            if not clicked:
                raise TencentFormError("未找到腾讯文档表单提交按钮。")
            session.save_storage_state(self.session_path)
            return {"ok": True, "provider": self.provider}
        finally:
            session.close()

    def verify_submission(self, schema: FormSchema, answer_key: str) -> dict[str, Any]:
        return {}


def classify_tencent_url(url: str) -> TencentDocumentType:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/form/" in path or "/forms/" in path:
        return "form"
    return "unsupported"


def page_needs_login(page: Any) -> bool:
    strong_login_markers = (
        "\u626b\u7801\u767b\u5f55",
        "\u5fae\u4fe1\u767b\u5f55",
        "qq\u767b\u5f55",
        "sign in",
        "鎵爜鐧诲綍",
        "寰俊鐧诲綍",
        "qq鐧诲綍",
    )
    return browser_page_needs_login(
        page,
        url_markers=("login", "passport"),
        strong_login_markers=strong_login_markers,
        fallback_login_markers=("\u767b\u5f55", "login", "鐧诲綍"),
        has_form_controls=has_tencent_form_controls,
    )


def is_login_complete(page: Any) -> bool:
    if has_tencent_strong_auth_cookie(page):
        return True
    if page_needs_login(page):
        return False
    if has_tencent_form_controls(page):
        return True
    return has_tencent_authenticated_signal(page)


def has_tencent_form_controls(page: Any) -> bool:
    return has_form_controls(page, TENCENT_FORM_CONTROL_SELECTOR)


def has_tencent_authenticated_signal(page: Any) -> bool:
    return has_authenticated_signal(page, script=AUTHENTICATED_PAGE_SCRIPT, cookie_names=TENCENT_AUTH_COOKIE_NAMES)


register_form_provider(
    HostFormProvider(
        name="tencent",
        label="腾讯文档收集表/表单",
        hosts=("docs.qq.com",),
        host_suffixes=(".docs.qq.com",),
        classify_url=classify_tencent_url,
        service_factory=lambda document_type: TencentFormService(document_type=document_type),
    )
)


def has_tencent_strong_auth_cookie(page: Any) -> bool:
    return has_auth_cookie(page, TENCENT_AUTH_COOKIE_NAMES)


def get_tencent_login_name(page: Any) -> str | None:
    value = read_login_name_from_page(page)
    if value:
        return value
    return read_login_name_from_tencent_account_page(page)


def read_login_name_from_page(page: Any) -> str | None:
    return read_browser_login_name_from_page(page, LOGIN_DISPLAY_NAME_SCRIPT)


def read_login_name_from_tencent_account_page(page: Any) -> str | None:
    return read_login_name_from_account_pages(
        page,
        account_urls=TENCENT_ACCOUNT_URLS,
        page_needs_login=page_needs_login,
        login_display_name_script=LOGIN_DISPLAY_NAME_SCRIPT,
    )


def safe_page_title(page: Any) -> str | None:
    return browser_safe_page_title(page)


def extract_questions_from_page(page: Any) -> list[dict[str, Any]]:
    return extract_browser_questions_from_page(page, EXTRACT_SCRIPT)


def wait_for_tencent_form_ready(page: Any) -> None:
    wait_for_form_ready(page, TENCENT_FORM_CONTROL_SELECTOR)


def parse_tencent_questions(raw_questions: list[dict[str, Any]], *, source_url: str, title: str) -> FormSchema:
    return build_browser_form_schema(
        raw_questions,
        source_url=source_url,
        title=title,
        provider="tencent",
        raw_questions_key="tencent_questions",
        default_form_id="tencent-form",
    )


def normalize_tencent_kind(raw_kind: str, options: list[dict[str, Any]]) -> str:
    return normalize_browser_question_kind(raw_kind, options)


def extract_tencent_form_id(url: str) -> str:
    return extract_form_id(url, "tencent-form")


def clean_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("*", "").replace("必填", "").strip()


AUTHENTICATED_PAGE_SCRIPT = r"""
() => {
  const visible = (node) => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity || "1") > 0.05;
  };
  const text = (document.body && (document.body.innerText || document.body.textContent || "").toLowerCase()) || "";
  const loginTerms = ["\u767b\u5f55", "\u626b\u7801\u767b\u5f55", "\u5fae\u4fe1\u767b\u5f55", "qq\u767b\u5f55", "sign in", "login"];
  if (loginTerms.some((term) => text.includes(term))) return false;
  const accountTerms = ["\u9000\u51fa\u767b\u5f55", "\u4e2a\u4eba\u4e2d\u5fc3", "\u8d26\u53f7\u8bbe\u7f6e", "\u8d26\u6237\u8bbe\u7f6e", "sign out", "logout"];
  if (accountTerms.some((term) => text.includes(term))) return true;
  return Array.from(document.querySelectorAll(
    "[class*='avatar'], [class*='user-avatar'], [class*='account'], [class*='profile'], [aria-label*='\u8d26\u53f7'], [title*='\u8d26\u53f7']"
  )).some(visible);
}
"""


LOGIN_DISPLAY_NAME_SCRIPT = r"""
() => {
  const visible = (node) => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity || "1") > 0.05;
  };
  const clean = (value) => {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    if (!text) return "";
    const rejected = ["\u767b\u5f55", "\u626b\u7801", "\u5fae\u4fe1", "qq\u767b\u5f55", "sign in", "login", "\u9000\u51fa\u767b\u5f55", "logout"];
    if (rejected.some((term) => text.toLowerCase().includes(term.toLowerCase()))) return "";
    return text
      .replace(/^\u8d26\u53f7[:：]\s*/, "")
      .replace(/^\u7528\u6237[:：]\s*/, "")
      .replace(/^\u6635\u79f0[:：]\s*/, "")
      .replace(/\s*\u4e2a\u4eba\u4e2d\u5fc3\s*$/, "")
      .replace(/\s*\u8d26\u53f7\u8bbe\u7f6e\s*$/, "")
      .trim();
  };
  const nodes = Array.from(document.querySelectorAll(
    "[class*='avatar'], [class*='user-avatar'], [class*='userName'], [class*='username'], [class*='account'], [class*='profile'], [aria-label*='\u8d26\u53f7'], [title*='\u8d26\u53f7'], [title*='\u7528\u6237']"
  )).filter(visible);
  for (const node of nodes) {
    const values = [
      node.getAttribute("data-name"),
      node.getAttribute("data-user-name"),
      node.getAttribute("title"),
      node.getAttribute("aria-label"),
      node.getAttribute("alt"),
      node.innerText,
      node.textContent
    ];
    for (const value of values) {
      const name = clean(value);
      if (name && name.length <= 80) return name;
    }
  }
  return null;
}
"""


EXTRACT_SCRIPT = r"""
() => {
  const textOf = (node) => (node && (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim()) || '';
  const clean = (text) => text.replace(/\*/g, '').replace(/必填/g, '').replace(/\s+/g, ' ').trim();
  let candidates = Array.from(document.querySelectorAll('.question-main'));
  if (!candidates.length) {
    candidates = Array.from(document.querySelectorAll(
      '[data-form-killer-question], [data-question-id], [data-field-id], [class*="question"], [class*="form-item"], [class*="field"]'
    ));
  }
  const blocks = candidates.filter((node) => {
    const controls = node.querySelectorAll('input, textarea, select, [role="radio"], [role="checkbox"]');
    return controls.length > 0 && textOf(node).length > 0;
  });
  const seen = new Set();
  return blocks.map((block, index) => {
    const control = block.querySelector('textarea, select, input, [role="radio"], [role="checkbox"]');
    const type = (control && (control.getAttribute('type') || control.getAttribute('role') || control.tagName)) || 'unsupported';
    let kind = type.toLowerCase();
    if (control && control.tagName === 'TEXTAREA') kind = 'textarea';
    if (control && control.tagName === 'SELECT') kind = 'select';

    const id = block.getAttribute('data-form-killer-question')
      || block.getAttribute('data-question-id')
      || block.getAttribute('data-field-id')
      || (control && (control.getAttribute('name') || control.getAttribute('id')))
      || `q_${index + 1}`;

    const labelNode = block.querySelector('[data-form-killer-label], .question-title, [class*="title"], [class*="label"], legend, label');
    const label = clean((labelNode && textOf(labelNode)) || textOf(block).split('\n')[0] || id);
    const required = block.getAttribute('data-required') === 'true'
      || !!block.querySelector('[required]')
      || !!block.querySelector('.required-span')
      || /必填|\*/.test(textOf(block));

    let options = [];
    if (kind === 'select') {
      options = Array.from(block.querySelectorAll('option')).filter((option) => option.value || textOf(option)).map((option, optionIndex) => ({
        id: option.value || `${optionIndex + 1}`,
        label: textOf(option),
      }));
    } else if (kind === 'radio' || kind === 'checkbox') {
      options = Array.from(block.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]')).map((option, optionIndex) => {
        const optionId = option.getAttribute('value') || option.getAttribute('data-option-id') || `${optionIndex + 1}`;
        const optionLabel = option.getAttribute('aria-label')
          || textOf(option.closest('label'))
          || textOf(option.parentElement)
          || optionId;
        return { id: optionId, label: clean(optionLabel) };
      });
    }

    const key = `${id}:${label}`;
    if (seen.has(key)) return null;
    seen.add(key);
    return { id, label, kind, required, options, dom_index: index, selector: block.getAttribute('data-form-killer-question') ? `[data-form-killer-question="${id}"]` : null };
  }).filter(Boolean);
}
"""


FILL_SCRIPT = r"""
({ values, questions }) => {
  const textOf = (node) => (node && (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim()) || '';
  const blocks = Array.from(document.querySelectorAll(
    '[data-form-killer-question], [data-question-id], [data-field-id], [class*="question"], [class*="form-item"], [class*="field"]'
  )).filter((node) => node.querySelectorAll('input, textarea, select, [role="radio"], [role="checkbox"]').length > 0);

  for (const question of questions) {
    if (!(question.id in values)) continue;
    const value = values[question.id];
    const block = question.selector ? document.querySelector(question.selector) : blocks[question.dom_index];
    if (!block) return { ok: false, error: `未找到字段：${question.label}` };

    if (question.kind === 'string' || question.kind === 'text' || question.kind === 'integer') {
      const input = block.querySelector('textarea, input:not([type]), input[type="text"], input[type="number"]');
      if (!input) return { ok: false, error: `未找到输入框：${question.label}` };
      input.focus();
      input.value = String(value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      continue;
    }

    if (question.kind === 'dropdown') {
      const select = block.querySelector('select');
      if (!select) return { ok: false, error: `未找到下拉框：${question.label}` };
      const selected = Array.isArray(value) ? value[0] : value;
      select.value = String(selected);
      select.dispatchEvent(new Event('change', { bubbles: true }));
      continue;
    }

    if (question.kind === 'enum' || question.kind === 'checkbox') {
      const wanted = new Set((Array.isArray(value) ? value : [value]).map(String));
      const controls = Array.from(block.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]'));
      for (const control of controls) {
        const controlValue = control.getAttribute('value') || control.getAttribute('data-option-id') || textOf(control.closest('label')) || textOf(control.parentElement);
        const label = textOf(control.closest('label')) || textOf(control.parentElement);
        if (wanted.has(String(controlValue)) || wanted.has(label)) {
          control.click();
        }
      }
      continue;
    }

    if (question.kind === 'boolean') {
      const control = block.querySelector('input[type="checkbox"], [role="checkbox"]');
      if (!control) return { ok: false, error: `未找到布尔控件：${question.label}` };
      const shouldCheck = Boolean(value);
      const checked = control.checked || control.getAttribute('aria-checked') === 'true';
      if (shouldCheck !== checked) control.click();
    }
  }
  return { ok: true };
}
"""


def click_submit_button(page: Any) -> bool:
    locators = [
        'button:has-text("提交")',
        'button:has-text("完成")',
        '[role="button"]:has-text("提交")',
        '[role="button"]:has-text("完成")',
        'button[type="submit"]',
    ]
    for selector in locators:
        try:
            locator = page.locator(selector).first
            if callable(locator):
                locator = locator()
            if locator.count() > 0:
                locator.click()
                return True
        except Exception:
            continue
    return False
