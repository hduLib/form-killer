from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from form_killer.events import ServiceEvent, ServiceEventHandler
from form_killer.forms.base import FormSchema, FormServiceError, Option, Question
from form_killer.forms.services import HostFormProvider, register_form_provider


class YandexFormError(FormServiceError):
    """Raised when Yandex Forms cannot be parsed or submitted."""


class YandexCaptchaError(YandexFormError):
    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(
            "Yandex 返回了 SmartCaptcha 验证页。CLI 不能自动求解或绕过 CAPTCHA；"
            "将把当前会话交给你在浏览器中手动完成验证。"
        )


class YandexFormAdapter:
    def __init__(self, *, timeout: float = 30.0, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._base_url = "https://forms.yandex.ru"
        self._sdk_endpoint = "/u/gateway"
        self._csrf_token: str | None = None
        self._lang = "ru"
        self._yandexuid: str | None = None

    def fetch_schema(self, url: str) -> FormSchema:
        survey_id = extract_survey_id(url)
        self._bootstrap(url)
        response = self._post_gateway("root/form/getSurvey", {"surveyId": survey_id}, referer=url)
        return parse_survey(response)

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        if not path.exists() or not path.is_file():
            raise YandexFormError(f"File does not exist: {path}")

        with path.open("rb") as handle:
            files = {"file": (path.name, handle)}
            data = {"surveyId": schema.id}
            headers = self._headers()
            headers.pop("content-type", None)
            response = self.client.post(
                self._gateway_url("root/form/uploadFile"),
                data=data,
                files=files,
                headers=headers,
            )
        return self._decode_response(response)

    def submit(self, schema: FormSchema, values: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "surveyId": schema.id,
            "values": values,
            "parent": "",
            "dryRun": dry_run,
        }
        return self._post_gateway("root/form/postSurvey", payload)

    def get_success(self, schema_or_survey_id: FormSchema | str, answer_key: str) -> dict[str, Any]:
        survey_id = schema_or_survey_id.id if isinstance(schema_or_survey_id, FormSchema) else schema_or_survey_id
        return self._post_gateway(
            "root/form/getSuccess",
            {"surveyId": survey_id, "answerKey": answer_key},
        )

    def _bootstrap(self, url: str) -> None:
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "ru,en;q=0.9",
            "user-agent": user_agent(),
        }
        last_status: int | None = None
        attempts = 6
        for attempt in range(attempts):
            response = self.client.get(url, headers=headers)
            last_status = response.status_code
            if response.status_code >= 400:
                raise YandexFormError(f"Yandex returned HTTP {response.status_code} while loading form.")

            html = response.text
            if _looks_like_captcha(html):
                raise YandexCaptchaError(url)
            self._base_url = origin_from_url(str(response.url))
            self._csrf_token = _search_json_string(html, "csrfToken") or _search_meta_content(html, "csrf-token")
            self._sdk_endpoint = _search_json_string(html, "sdkEndpoint") or "/u/gateway"
            self._lang = _search_json_string(html, "lang") or "ru"
            self._yandexuid = _search_json_string(html, "yandexuid")
            if self._csrf_token:
                return
            if attempt < attempts - 1:
                time.sleep(0.5)

        raise YandexFormError(
            f"Could not find Yandex CSRF token after loading form"
            f"{'' if last_status is None else f' (HTTP {last_status})'}. "
            "The form may require login or changed markup."
        )

    def _post_gateway(self, path: str, payload: dict[str, Any], *, referer: str | None = None) -> dict[str, Any]:
        response = self.client.post(
            self._gateway_url(path),
            json=payload,
            headers=self._headers(referer=referer),
        )
        return self._decode_response(response)

    def _gateway_url(self, path: str) -> str:
        endpoint = self._sdk_endpoint.rstrip("/") + "/" + path.lstrip("/")
        return urljoin(self._base_url, endpoint)

    def _headers(self, *, referer: str | None = None) -> dict[str, str]:
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": self._lang,
            "content-type": "application/json",
            "user-agent": user_agent(),
            "x-csrf-token": self._csrf_token or "",
        }
        if self._yandexuid:
            headers["x-forms-yandexuid"] = self._yandexuid
            headers["x-use-collab"] = "1"
        if referer:
            headers["referer"] = referer
        return headers

    def _decode_response(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code in {401, 403}:
            raise YandexFormError("Access denied. This form may require login or organization access.")
        if response.status_code == 419:
            token = response.headers.get("x-csrf-token")
            if token:
                self._csrf_token = token
            raise YandexFormError("Yandex rejected the CSRF token. Please retry; the session may have expired.")
        if response.status_code >= 400:
            raise YandexFormError(f"Yandex returned HTTP {response.status_code}: {response.text[:300]}")

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise YandexFormError("Yandex returned a non-JSON response.") from exc
        if isinstance(data, dict) and data.get("code") in {"not_permitted", "access_denied"}:
            raise YandexFormError("Yandex says this form is not permitted for the current session.")
        if not isinstance(data, dict):
            raise YandexFormError("Yandex returned an unexpected response shape.")
        return data

    def export_browser_cookies(self) -> list[dict[str, Any]]:
        cookies: list[dict[str, Any]] = []
        for cookie in self.client.cookies.jar:
            item: dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain or urlparse(self._base_url).hostname or "forms.yandex.ru",
                "path": cookie.path or "/",
                "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly")),
                "secure": bool(cookie.secure),
            }
            if cookie.expires is not None:
                item["expires"] = cookie.expires
            cookies.append(item)
        return cookies

    def import_browser_cookies(self, cookies: list[dict[str, Any]]) -> None:
        for cookie in cookies:
            domain = str(cookie.get("domain") or "forms.yandex.ru")
            path = str(cookie.get("path") or "/")
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name:
                self.client.cookies.set(name, value, domain=domain, path=path)


class YandexFormService:
    provider = "yandex"
    label = "Yandex Forms"
    document_type = "form"
    supports_login = False
    login_required_error = None

    def __init__(self, adapter: YandexFormAdapter | None = None) -> None:
        self.adapter = adapter or YandexFormAdapter()

    def fetch_schema(self, url: str, event_handler: ServiceEventHandler | None = None) -> FormSchema:
        if event_handler:
            event_handler(ServiceEvent("parse_start", provider=self.provider, provider_label=self.label))
        schema = self.adapter.fetch_schema(url)
        return schema.model_copy(
            update={
                "raw": {
                    **schema.raw,
                    "provider": self.provider,
                    "document_type": self.document_type,
                    "source_url": url,
                }
            }
        )

    def login(self, *args: Any, **kwargs: Any) -> str | None:
        raise YandexFormError("Yandex Forms 当前不支持 CLI 登录流程。")

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        return self.adapter.upload_file(schema, question, path)

    def submit(
        self,
        schema: FormSchema,
        values: dict[str, Any],
        *,
        dry_run: bool,
        event_handler: ServiceEventHandler | None = None,
    ) -> dict[str, Any]:
        if event_handler:
            event_handler(
                ServiceEvent(
                    "dry_run_start" if dry_run else "submit_start",
                    provider=self.provider,
                    provider_label=self.label,
                )
            )
        return self.adapter.submit(schema, values, dry_run=dry_run)

    def verify_submission(self, schema: FormSchema, answer_key: str) -> dict[str, Any]:
        return self.adapter.get_success(schema, answer_key)

    def export_browser_cookies(self) -> list[dict[str, Any]]:
        return self.adapter.export_browser_cookies()

    def import_browser_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.adapter.import_browser_cookies(cookies)


def parse_survey(data: dict[str, Any]) -> FormSchema:
    questions: list[Question] = []
    for page in data.get("pages", []):
        for item in page.get("items", []):
            if item.get("hidden"):
                continue
            questions.append(parse_question(item))
    return FormSchema(
        id=str(data["id"]),
        name=str(data.get("name") or data["id"]),
        submit_text=str(data.get("texts", {}).get("submit") or "Submit"),
        questions=questions,
        raw=data,
    )


def parse_question(item: dict[str, Any]) -> Question:
    raw_type = str(item.get("type") or "unsupported")
    widget = item.get("widget")
    validations = list(item.get("validations") or [])
    required = any(validation.get("type") == "required" for validation in validations)

    kind = normalize_kind(raw_type, widget, item.get("multiline"))
    max_count = _validation_value(validations, "count")
    max_size = _validation_value(validations, "size")

    options = [
        Option(id=str(option["id"]), label=str(option.get("label") or option["id"]))
        for option in item.get("items", [])
        if "id" in option
    ]
    return Question(
        id=str(item["id"]),
        label=str(item.get("label") or item["id"]),
        kind=kind,
        raw_type=raw_type,
        widget=str(widget) if widget else None,
        required=required,
        hidden=bool(item.get("hidden", False)),
        multiline=bool(item.get("multiline", False)),
        options=options,
        validations=validations,
        max_file_count=max_count if isinstance(max_count, int) else None,
        max_file_size_mb=max_size if isinstance(max_size, int) else None,
    )


def normalize_kind(raw_type: str, widget: Any, multiline: Any = False) -> str:
    if raw_type == "string":
        return "text" if multiline else "string"
    if raw_type == "enum":
        if widget == "checkbox":
            return "checkbox"
        if widget == "dropdown":
            return "dropdown"
        return "enum"
    if raw_type in {"boolean", "integer", "file"}:
        return raw_type
    return "unsupported"


def extract_survey_id(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return url.strip("/")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "u":
        return parts[1]
    if parts:
        return parts[-1]
    raise YandexFormError(f"Could not extract survey id from URL: {url}")


def origin_from_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def user_agent() -> str:
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )


def _search_json_string(html: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', html)
    if not match:
        return None
    return json.loads(f'"{match.group(1)}"')


def _search_meta_content(html: str, name: str) -> str | None:
    match = re.search(
        rf'<meta\s+[^>]*name=["\']{re.escape(name)}["\'][^>]*content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _looks_like_captcha(html: str) -> bool:
    lowered = html.lower()
    return "captcha_smart" in lowered or "smart-captcha" in lowered or "captcha" in lowered and "csrf-token" not in lowered


def _validation_value(validations: list[dict[str, Any]], validation_type: str) -> Any:
    for validation in validations:
        if validation.get("type") == validation_type:
            return validation.get("value")
    return None


register_form_provider(
    HostFormProvider(
        name="yandex",
        label="Yandex Forms",
        hosts=("forms.yandex.ru",),
        host_suffixes=(".forms.yandex.ru",),
        classify_url=lambda _url: "form",
        service_factory=lambda _document_type: YandexFormService(),
    )
)
