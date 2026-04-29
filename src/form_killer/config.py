from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Literal

APP_NAME = "form-killer"
PROJECT_CACHE_DIR_NAME = ".form-killer"
BASE_URL_KEY = "openai_base_url"
MODEL_KEY = "openai_model"
DEFAULT_MODEL = "gpt-5.5"
TENCENT_SESSION_PATH_KEY = "tencent_session_path"
TENCENT_LOGIN_TIMEOUT_SECONDS_KEY = "tencent_login_timeout_seconds"
TENCENT_HEADLESS_KEY = "tencent_headless"
WPS_SESSION_PATH_KEY = "wps_session_path"
WPS_LOGIN_TIMEOUT_SECONDS_KEY = "wps_login_timeout_seconds"
WPS_HEADLESS_KEY = "wps_headless"
DEFAULT_TENCENT_LOGIN_TIMEOUT_SECONDS = 180
DEFAULT_TENCENT_HEADLESS = False
DEFAULT_WPS_LOGIN_TIMEOUT_SECONDS = 180
DEFAULT_WPS_HEADLESS = False


ApiKeySource = Literal["cli", "env", "config", "missing"]


def get_project_cache_dir() -> Path:
    configured = os.getenv("FORM_KILLER_PROJECT_DIR")
    root = Path(configured).expanduser() if configured else Path.cwd()
    return root.resolve() / PROJECT_CACHE_DIR_NAME


def get_config_path() -> Path:
    return get_project_cache_dir() / "config.toml"


def get_legacy_config_path() -> Path:
    return get_project_cache_dir() / "config.json"


def get_default_tencent_session_path() -> Path:
    return get_project_cache_dir() / "sessions" / "tencent.json"


def get_default_wps_session_path() -> Path:
    return get_project_cache_dir() / "sessions" / "wps.json"


def ensure_config_file() -> Path:
    path = get_config_path()
    if not path.exists():
        _write_config_file(_read_config_file())
    return path


def save_api_key(api_key: str, *, allow_plaintext: bool = False) -> str:
    path = get_config_path()
    data = _read_config_file()
    data["openai_api_key"] = api_key
    _write_config_file(data)
    return str(path)


def load_api_key() -> str | None:
    value, _source = load_api_key_with_source()
    return value


def load_api_key_with_source() -> tuple[str | None, ApiKeySource]:
    data = _read_config_file()
    value = data.get("openai_api_key")
    if value:
        return str(value), "config"
    return None, "missing"


def save_base_url(base_url: str | None) -> str:
    data = _read_config_file()
    if base_url:
        data[BASE_URL_KEY] = base_url.rstrip("/")
    else:
        data.pop(BASE_URL_KEY, None)
    _write_config_file(data)
    return str(get_config_path())


def load_base_url() -> str | None:
    value = _read_config_file().get(BASE_URL_KEY)
    return str(value) if value else None


def save_model(model: str | None) -> str:
    data = _read_config_file()
    if model:
        data[MODEL_KEY] = model
    else:
        data.pop(MODEL_KEY, None)
    _write_config_file(data)
    return str(get_config_path())


def load_model() -> str | None:
    value = _read_config_file().get(MODEL_KEY)
    return str(value) if value else None


def save_tencent_session_path(path: str | Path | None) -> str:
    data = _read_config_file()
    if path:
        data[TENCENT_SESSION_PATH_KEY] = str(path)
    else:
        data.pop(TENCENT_SESSION_PATH_KEY, None)
    _write_config_file(data)
    return str(get_config_path())


def load_tencent_session_path() -> Path:
    return _load_path(TENCENT_SESSION_PATH_KEY, get_default_tencent_session_path())


def save_tencent_login_timeout_seconds(seconds: int | None) -> str:
    data = _read_config_file()
    if seconds is None:
        data.pop(TENCENT_LOGIN_TIMEOUT_SECONDS_KEY, None)
    else:
        data[TENCENT_LOGIN_TIMEOUT_SECONDS_KEY] = str(seconds)
    _write_config_file(data)
    return str(get_config_path())


def load_tencent_login_timeout_seconds() -> int:
    return _load_positive_int(TENCENT_LOGIN_TIMEOUT_SECONDS_KEY, DEFAULT_TENCENT_LOGIN_TIMEOUT_SECONDS)


def save_tencent_headless(value: bool | None) -> str:
    data = _read_config_file()
    if value is None:
        data.pop(TENCENT_HEADLESS_KEY, None)
    else:
        data[TENCENT_HEADLESS_KEY] = "true" if value else "false"
    _write_config_file(data)
    return str(get_config_path())


def load_tencent_headless() -> bool:
    return _load_bool(TENCENT_HEADLESS_KEY, DEFAULT_TENCENT_HEADLESS)


def load_wps_session_path() -> Path:
    return _load_path(WPS_SESSION_PATH_KEY, get_default_wps_session_path())


def load_wps_login_timeout_seconds() -> int:
    return _load_positive_int(WPS_LOGIN_TIMEOUT_SECONDS_KEY, DEFAULT_WPS_LOGIN_TIMEOUT_SECONDS)


def load_wps_headless() -> bool:
    return _load_bool(WPS_HEADLESS_KEY, DEFAULT_WPS_HEADLESS)


def clear_api_key() -> list[str]:
    cleared: list[str] = []
    path = get_config_path()
    data = _read_config_file()
    if "openai_api_key" in data:
        data.pop("openai_api_key", None)
        _write_config_file(data)
        cleared.append(str(path))
    return cleared


def _load_path(key: str, default: Path) -> Path:
    value = _read_config_file().get(key)
    return Path(os.path.expandvars(str(value))).expanduser() if value else default


def _load_positive_int(key: str, default: int) -> int:
    value = _read_config_file().get(key)
    if value is None:
        return default
    try:
        parsed = int(str(value))
    except ValueError:
        return default
    return max(1, parsed)


def _load_bool(key: str, default: bool) -> bool:
    value = _read_config_file().get(key)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _read_config_file() -> dict[str, str]:
    path = get_config_path()
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return {}
        return _normalize_config(data)

    legacy_path = get_legacy_config_path()
    if legacy_path.exists():
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return _normalize_config(data)
    return {}


def _normalize_config(data: object) -> dict[str, str]:
    return data if isinstance(data, dict) else {}


def _write_config_file(data: dict[str, str]) -> None:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_config(data), encoding="utf-8")


def _render_config(data: dict[str, str]) -> str:
    lines = [
        "# form-killer 配置文件",
        "# 本文件默认保存在项目目录 .form-killer/config.toml。",
        "# 你可以直接编辑这个文件；保存后下次运行 form-killer 会自动读取。",
        "#",
        "# 优先级：命令行参数 > 环境变量 > 本配置文件 > 默认值。",
        "# 环境变量：OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL。",
        "#",
        "# 安全提示：API Key 会明文保存在项目本地配置中；.form-killer/ 应保持在 .gitignore 中。",
    ]
    api_key = data.get("openai_api_key")
    if api_key:
        lines.append(f'openai_api_key = "{_toml_escape(str(api_key))}"')
    else:
        lines.append('# openai_api_key = "sk-..."')

    lines.extend(
        [
            "",
            "# OpenAI 兼容接口 Base URL。",
            '# 官方 OpenAI 可留空；第三方兼容接口示例："https://api.example.com/v1"',
        ]
    )
    base_url = data.get(BASE_URL_KEY)
    if base_url:
        lines.append(f'{BASE_URL_KEY} = "{_toml_escape(str(base_url))}"')
    else:
        lines.append(f'# {BASE_URL_KEY} = "https://api.openai.com/v1"')

    lines.extend(
        [
            "",
            "# 默认模型。交互流程会展示这个默认值，也可以临时输入其他模型。",
        ]
    )
    model = data.get(MODEL_KEY) or DEFAULT_MODEL
    lines.append(f'{MODEL_KEY} = "{_toml_escape(str(model))}"')
    lines.extend(
        [
            "",
            "# 腾讯文档登录会话。首次登录后会保存 Playwright storage_state，后续复用。",
            "# 如果你想更换账号，可以删除这个文件或改成新的路径。",
        ]
    )
    session_path = data.get(TENCENT_SESSION_PATH_KEY)
    if session_path:
        lines.append(f'{TENCENT_SESSION_PATH_KEY} = "{_toml_escape(str(session_path))}"')
    else:
        lines.append(f'# {TENCENT_SESSION_PATH_KEY} = "{_toml_escape(str(get_default_tencent_session_path()))}"')
    lines.extend(
        [
            "",
            "# 腾讯登录等待时间，单位秒。",
        ]
    )
    login_timeout = data.get(TENCENT_LOGIN_TIMEOUT_SECONDS_KEY) or str(DEFAULT_TENCENT_LOGIN_TIMEOUT_SECONDS)
    lines.append(f'{TENCENT_LOGIN_TIMEOUT_SECONDS_KEY} = "{_toml_escape(str(login_timeout))}"')
    lines.extend(
        [
            "",
            "# 腾讯文档浏览器是否无头运行。登录通常需要可见浏览器，所以默认 false。",
        ]
    )
    headless = data.get(TENCENT_HEADLESS_KEY) or ("true" if DEFAULT_TENCENT_HEADLESS else "false")
    lines.append(f'{TENCENT_HEADLESS_KEY} = "{_toml_escape(str(headless).lower())}"')
    lines.extend(
        [
            "",
            "# WPS/金山表单登录会话。首次登录后会保存 Playwright storage_state，后续复用。",
        ]
    )
    wps_session_path = data.get(WPS_SESSION_PATH_KEY)
    if wps_session_path:
        lines.append(f'{WPS_SESSION_PATH_KEY} = "{_toml_escape(str(wps_session_path))}"')
    else:
        lines.append(f'# {WPS_SESSION_PATH_KEY} = "{_toml_escape(str(get_default_wps_session_path()))}"')
    lines.append(f'{WPS_LOGIN_TIMEOUT_SECONDS_KEY} = "{DEFAULT_WPS_LOGIN_TIMEOUT_SECONDS}"')
    lines.append(f'{WPS_HEADLESS_KEY} = "{str(DEFAULT_WPS_HEADLESS).lower()}"')
    lines.append("")
    return "\n".join(lines)


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
