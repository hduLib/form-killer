from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from form_killer.config import load_wps_headless, load_wps_login_timeout_seconds, load_wps_session_path
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


WpsDocumentType = Literal["form", "unsupported"]
WPS_FORM_CONTROL_SELECTOR = ".ksapc-questions-write-container, textarea, input[type='radio'], input[type='checkbox']"
WPS_AUTH_COOKIE_NAMES = {"wps_sid", "wps_sid_sso", "wps_sso", "kso_sid"}
WPS_ACCOUNT_URLS = ("https://account.wps.cn/usercenter/", "https://www.kdocs.cn/latest")


class WpsFormError(FormServiceError):
    pass


class WpsUnsupportedDocumentError(WpsFormError):
    pass


class WpsLoginRequired(FormLoginRequired, WpsFormError):
    def __init__(self, url: str) -> None:
        super().__init__(url, "WPS/金山表单需要登录。工具会在后台获取二维码/验证码并输出到 CLI，请你完成登录后继续。")


class WpsBrowserSession(BrowserFormSession):
    @classmethod
    def open(cls, *, storage_path: Path, headless: bool) -> "WpsBrowserSession":
        return cls.open_for_provider(
            storage_path=storage_path,
            headless=headless,
            error_type=WpsFormError,
            missing_playwright_message="缺少 Playwright，无法打开 WPS/金山表单浏览器。",
        )


class WpsFormService:
    provider = "wps"
    label = "WPS/金山表单"
    supports_login = True
    login_required_error = WpsLoginRequired

    def __init__(
        self,
        *,
        document_type: WpsDocumentType = "form",
        session_path: Path | None = None,
        login_timeout_seconds: int | None = None,
        headless: bool | None = None,
        browser_session_cls: type[WpsBrowserSession] = WpsBrowserSession,
    ) -> None:
        self.document_type = document_type
        self.session_path = session_path or load_wps_session_path()
        self.login_timeout_seconds = login_timeout_seconds or load_wps_login_timeout_seconds()
        self.headless = load_wps_headless() if headless is None else headless
        self.browser_session_cls = browser_session_cls

    def fetch_schema(self, url: str, event_handler: ServiceEventHandler | None = None) -> FormSchema:
        if self.document_type != "form":
            raise WpsUnsupportedDocumentError("暂不支持该 WPS/金山文档类型。当前仅支持 WPS/金山表单。")

        if event_handler:
            event_handler(ServiceEvent("parse_start", provider=self.provider, provider_label=self.label))
        session = self.browser_session_cls.open(storage_path=self.session_path, headless=self.headless)
        try:
            session.page.goto(url, wait_until="networkidle", timeout=60_000)
            wait_for_wps_form_ready(session.page)
            title = safe_page_title(session.page) or "WPS Form"
            raw_questions = extract_questions_from_page(session.page)
            if not raw_questions and page_needs_login(session.page):
                raise WpsLoginRequired(url)
            if not raw_questions:
                raise WpsFormError("未能从 WPS/金山页面识别到表单题目。请确认这是表单填写链接。")
            session.save_storage_state(self.session_path)
            return parse_wps_questions(raw_questions, source_url=url, title=title)
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
            get_login_name=get_wps_login_name,
            login_required_type=WpsLoginRequired,
            timeout_message="WPS/金山文档 login timed out. Please run login again.",
            challenge_image_bytes=login_challenge_image_bytes,
            wait_for_user=wait_for_user,
            login_runner=login_runner,
            event_handler=event_handler,
        )

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        raise WpsFormError("WPS/金山表单 v1 暂不支持文件上传字段。")

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
            raise WpsFormError("缺少 WPS/金山表单来源 URL，无法提交。")

        if event_handler:
            event_handler(ServiceEvent("fill_start", provider=self.provider, provider_label=self.label))
        session = self.browser_session_cls.open(storage_path=self.session_path, headless=self.headless)
        try:
            session.page.goto(source_url, wait_until="networkidle", timeout=60_000)
            wait_for_wps_form_ready(session.page)
            if page_needs_login(session.page):
                raise WpsLoginRequired(source_url)
            fill_result = session.page.evaluate(
                FILL_SCRIPT,
                {"values": values, "questions": schema.raw.get("wps_questions") or []},
            )
            if not fill_result.get("ok"):
                raise WpsFormError(str(fill_result.get("error") or "WPS/金山表单填写失败。"))
            if not click_submit_button(session.page):
                raise WpsFormError("未找到 WPS/金山表单提交按钮。")
            session.save_storage_state(self.session_path)
            return {"ok": True, "provider": self.provider}
        finally:
            session.close()

    def verify_submission(self, schema: FormSchema, answer_key: str) -> dict[str, Any]:
        return {}


def classify_wps_url(url: str) -> WpsDocumentType:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if host == "f.wps.cn" or host.endswith(".f.wps.cn"):
        return "form"
    if "ksform" in path or "/form" in path or "/g/" in path:
        return "form"
    return "unsupported"


def page_needs_login(page: Any) -> bool:
    strong_login_markers = (
        "\u626b\u7801\u767b\u5f55",
        "\u5fae\u4fe1\u767b\u5f55",
        "\u8d26\u53f7\u767b\u5f55",
        "\u91d1\u5c71\u8d26\u53f7",
        "\u4f7f\u7528\u91d1\u5c71\u529e\u516c\u5728\u7ebf\u670d\u52a1\u8d26\u53f7\u767b\u5f55",
        "sign in",
        "鎵爜鐧诲綍",
        "寰俊鐧诲綍",
        "璐﹀彿鐧诲綍",
        "閲戝北璐﹀彿",
    )
    return browser_page_needs_login(
        page,
        url_markers=("login", "account.wps", "passport"),
        strong_login_markers=strong_login_markers,
        fallback_login_markers=("\u767b\u5f55", "login", "鐧诲綍"),
        has_form_controls=has_wps_form_controls,
    )


def is_login_complete(page: Any) -> bool:
    if has_wps_strong_auth_cookie(page):
        return True
    if page_needs_login(page):
        return False
    if has_wps_form_controls(page):
        return True
    return has_wps_authenticated_signal(page)


def has_wps_form_controls(page: Any) -> bool:
    return has_form_controls(page, WPS_FORM_CONTROL_SELECTOR)


def has_wps_authenticated_signal(page: Any) -> bool:
    return has_authenticated_signal(page, script=AUTHENTICATED_PAGE_SCRIPT, cookie_names=WPS_AUTH_COOKIE_NAMES)


register_form_provider(
    HostFormProvider(
        name="wps",
        label="WPS/金山表单",
        hosts=("f.wps.cn", "www.kdocs.cn", "kdocs.cn"),
        host_suffixes=(".wps.cn", ".kdocs.cn"),
        classify_url=classify_wps_url,
        service_factory=lambda document_type: WpsFormService(document_type=document_type),
    )
)


def has_wps_strong_auth_cookie(page: Any) -> bool:
    return has_auth_cookie(page, WPS_AUTH_COOKIE_NAMES)


def get_wps_login_name(page: Any) -> str | None:
    value = read_login_name_from_page(page)
    if value:
        return value
    return read_login_name_from_wps_account_page(page)


def read_login_name_from_page(page: Any) -> str | None:
    return read_browser_login_name_from_page(page, LOGIN_DISPLAY_NAME_SCRIPT)


def read_login_name_from_wps_account_page(page: Any) -> str | None:
    return read_login_name_from_account_pages(
        page,
        account_urls=WPS_ACCOUNT_URLS,
        page_needs_login=page_needs_login,
        login_display_name_script=LOGIN_DISPLAY_NAME_SCRIPT,
    )


def safe_page_title(page: Any) -> str | None:
    return browser_safe_page_title(page)


def wait_for_wps_form_ready(page: Any) -> None:
    wait_for_form_ready(page, WPS_FORM_CONTROL_SELECTOR)


def extract_questions_from_page(page: Any) -> list[dict[str, Any]]:
    return extract_browser_questions_from_page(page, EXTRACT_SCRIPT)


def parse_wps_questions(raw_questions: list[dict[str, Any]], *, source_url: str, title: str) -> FormSchema:
    return build_browser_form_schema(
        raw_questions,
        source_url=source_url,
        title=title,
        provider="wps",
        raw_questions_key="wps_questions",
        default_form_id="wps-form",
    )


def normalize_wps_kind(raw_kind: str, options: list[dict[str, Any]]) -> str:
    return normalize_browser_question_kind(raw_kind, options)


def extract_wps_form_id(url: str) -> str:
    return extract_form_id(url, "wps-form")


AUTHENTICATED_PAGE_SCRIPT = r"""
() => {
  const visible = (node) => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    return rect.width > 8 && rect.height > 8 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity || "1") > 0.05;
  };
  const text = (document.body && (document.body.innerText || document.body.textContent || "").toLowerCase()) || "";
  const loginTerms = ["\u767b\u5f55", "\u626b\u7801\u767b\u5f55", "\u5fae\u4fe1\u767b\u5f55", "\u8d26\u53f7\u767b\u5f55", "\u91d1\u5c71\u8d26\u53f7", "sign in", "login"];
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
    const rejected = ["\u767b\u5f55", "\u626b\u7801", "\u5fae\u4fe1", "\u8d26\u53f7\u767b\u5f55", "\u91d1\u5c71\u8d26\u53f7", "sign in", "login", "\u9000\u51fa\u767b\u5f55", "logout"];
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
  let blocks = Array.from(document.querySelectorAll('.ksapc-questions-write-container'));
  if (!blocks.length) {
    blocks = Array.from(document.querySelectorAll('[class*="question"], [class*="form-item"], [class*="field"]')).filter((node) => {
      return node.querySelectorAll('input, textarea, select, [role="radio"], [role="checkbox"]').length > 0;
    });
  }
  const seen = new Set();
  return blocks.map((block, index) => {
    const labelNode = block.querySelector('.ksapc-question-title-title, [class*="question-title"], [class*="title"], legend, label');
    const label = clean((labelNode && textOf(labelNode)) || textOf(block).split('\n')[0] || `q_${index + 1}`);
    const required = block.className.includes('required') || !!block.querySelector('[required], .required-span, [class*="required"]') || /必填|\*/.test(textOf(block));
    const textarea = block.querySelector('textarea');
    const textInput = block.querySelector('input[type="text"], input[type="number"], input:not([type])');
    const radioInputs = Array.from(block.querySelectorAll('input[type="radio"], [role="radio"]'));
    const checkboxInputs = Array.from(block.querySelectorAll('input[type="checkbox"], [role="checkbox"]'));
    const select = block.querySelector('select');
    const scaleItems = Array.from(block.querySelectorAll('.ksapc-q-scale-select-level-item'));

    let kind = 'unsupported';
    let control = textarea || textInput || select || radioInputs[0] || checkboxInputs[0] || scaleItems[0];
    if (textarea) kind = 'textarea';
    else if (textInput) kind = (textInput.getAttribute('type') || 'text').toLowerCase();
    else if (select) kind = 'select';
    else if (radioInputs.length) kind = 'radio';
    else if (checkboxInputs.length) kind = 'checkbox';
    else if (scaleItems.length) kind = 'scale';

    const id = block.getAttribute('id')
      || block.getAttribute('data-question-id')
      || (control && (control.getAttribute && (control.getAttribute('name') || control.getAttribute('id'))))
      || `q_${index + 1}`;

    let options = [];
    if (select) {
      options = Array.from(block.querySelectorAll('option')).filter((option) => option.value || textOf(option)).map((option, optionIndex) => ({
        id: option.value || `${optionIndex + 1}`,
        label: textOf(option),
      }));
    } else if (radioInputs.length || checkboxInputs.length) {
      options = (radioInputs.length ? radioInputs : checkboxInputs).map((option, optionIndex) => {
        const label = option.closest('label');
        const optionId = option.getAttribute('value') || option.getAttribute('data-option-id') || `${optionIndex + 1}`;
        const optionLabel = option.getAttribute('aria-label')
          || textOf(label && label.querySelector('.ksapc-select-write-tile-select-val, .ksapc-vote-write-tile-item-val'))
          || textOf(label)
          || optionId;
        return { id: optionId || `${optionIndex + 1}`, label: clean(optionLabel.replace(/\s*\d+票\s*\d+%$/, '')) };
      });
    } else if (scaleItems.length) {
      options = scaleItems.map((item, optionIndex) => ({
        id: textOf(item) || `${optionIndex + 1}`,
        label: textOf(item) || `${optionIndex + 1}`,
      }));
    }

    const key = `${id}:${label}`;
    if (seen.has(key)) return null;
    seen.add(key);
    return { id, label, kind, required, options, dom_index: index, selector: block.id ? `#${CSS.escape(block.id)}` : null };
  }).filter(Boolean);
}
"""


FILL_SCRIPT = r"""
({ values, questions }) => {
  const textOf = (node) => (node && (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim()) || '';
  const blocks = Array.from(document.querySelectorAll('.ksapc-questions-write-container'));
  for (const question of questions) {
    if (!(question.id in values)) continue;
    const value = values[question.id];
    const block = question.selector ? document.querySelector(question.selector) : blocks[question.dom_index];
    if (!block) return { ok: false, error: `未找到字段：${question.label}` };

    if (question.kind === 'string' || question.kind === 'text' || question.kind === 'integer') {
      const input = block.querySelector('textarea, input[type="text"], input[type="number"], input:not([type])');
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
      select.value = String(Array.isArray(value) ? value[0] : value);
      select.dispatchEvent(new Event('change', { bubbles: true }));
      continue;
    }

    if (question.kind === 'enum' || question.kind === 'checkbox') {
      const wanted = new Set((Array.isArray(value) ? value : [value]).map(String));
      const controls = Array.from(block.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"], .ksapc-q-scale-select-level-item'));
      for (const control of controls) {
        const label = textOf(control.closest('label')) || textOf(control);
        const controlValue = control.getAttribute && (control.getAttribute('value') || control.getAttribute('data-option-id')) || label;
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
    for selector in [
        'button:has-text("提交")',
        'button:has-text("完成")',
        '[role="button"]:has-text("提交")',
        '[role="button"]:has-text("完成")',
        'button[type="submit"]',
    ]:
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
