# form-killer

中文 CLI 问卷答题工具，仅供学习参考。通过表单 provider 插件支持不同平台，并使用兼容 OpenAI API 格式的模型生成答案。

## 运行

在仓库内开发运行：

```powershell
uv run form-killer
```

从 Git 仓库临时运行：

```powershell
uvx --from "git+https://github.com/hduLib/form-killer.git" form-killer
```

拉取 Git 仓库最新内容后运行：

```powershell
uvx --refresh --from "https://github.com/hduLib/form-killer.git" form-killer
```

进入 CLI 后按提示操作：

1. 选择已注册的表单 provider。
2. 输入受支持平台的表单 URL。
3. 检查解析出的表单结构。
4. 按提示填写姓名、ID、邮箱、电话、地址、文件上传等用户输入字段。
5. 选择模型，默认使用已保存模型或 `gpt-5.5`。
6. 检查 AI 生成的答案预览。
7. 确认后提交。

## 配置

本地配置和缓存默认保存在项目目录 `.form-killer/`：

- `.form-killer/config.toml`：API Key、Base URL、模型和 provider 配置。
- `.form-killer/sessions/`：浏览器登录 session。

模型服务配置优先级：

1. 命令行参数：`--api-key`、`--base-url`
2. 环境变量：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`
3. 本地配置：`form-killer config set-openai-key`、`form-killer config set-openai-base-url`、`form-killer config set-openai-model`

## 预演

提交前先用 `--dry-run` 检查解析、答案生成、必填项和提交数据格式：

```powershell
uv run form-killer answer "表单URL" --dry-run
```

Provider 预演行为：

- Yandex Forms：使用平台接口的 `dryRun` 校验。
- 腾讯文档/WPS/金山表单：执行本地流程校验。
- 未知表单或 `--agent`：使用浏览器 Agent 将页面 DOM 转成通用 FormSchema，再按 schema 回填；遇到验证码会打开当前会话供人工验证，`--dry-run` 不点击提交。

确认提交时使用 `--submit`：

```powershell
uv run form-killer answer "表单URL" --submit
```

## Provider 插件开发

Provider 插件负责识别 URL、解析表单、上传文件、提交答案，以及处理登录流程。CLI 读取已注册 provider 列表，并通过服务事件输出日志、二维码和状态。

### 新增 provider

1. 新建模块，例如 `src/form_killer/forms/example.py`。
2. 在模块顶层调用 `register_form_provider(...)` 注册 provider。
3. 实现对应的表单 service。
4. 增加 provider 测试。

按域名匹配时使用 `HostFormProvider`：

```python
from form_killer.forms.services import HostFormProvider, register_form_provider

register_form_provider(
    HostFormProvider(
        name="example",
        label="Example Forms",
        hosts=("forms.example.com",),
        host_suffixes=(".forms.example.com",),
        classify_url=lambda url: "form",
        service_factory=lambda document_type: ExampleFormService(document_type=document_type),
    )
)
```

按自定义规则匹配时实现 provider 的 `resolve()`：

```python
from form_killer.forms.services import RoutedFormService, register_form_provider


class ExampleProvider:
    name = "example"
    label = "Example Forms"

    def resolve(self, url: str):
        if url.startswith("example://"):
            return RoutedFormService(
                provider=self.name,
                document_type="form",
                service=ExampleFormService(document_type="form"),
            )
        return None


register_form_provider(ExampleProvider())
```

启动时会扫描 `form_killer.forms` 包内的 provider 模块，并执行模块顶层的注册代码。

### 实现 service

服务类实现 `FormService` 协议常用方法：

- `fetch_schema(...)`：解析表单并返回 `FormSchema`。
- `login(...)`：处理登录并保存 session。
- `upload_file(...)`：上传文件并返回平台需要的文件值。
- `submit(...)`：执行预演校验或正式提交。
- `verify_submission(...)`：按提交结果做后续验证。

示例骨架：

```python
from pathlib import Path
from typing import Any

from form_killer.events import ServiceEvent, ServiceEventHandler
from form_killer.forms.base import FormLoginRequired, FormSchema, Question


class ExampleLoginRequired(FormLoginRequired):
    def __init__(self, url: str) -> None:
        super().__init__(url, "Example Forms 需要登录。")


class ExampleFormService:
    provider = "example"
    label = "Example Forms"
    supports_login = True
    login_required_error = ExampleLoginRequired

    def __init__(self, *, document_type: str = "form") -> None:
        self.document_type = document_type

    def fetch_schema(self, url: str, event_handler: ServiceEventHandler | None = None) -> FormSchema:
        if event_handler:
            event_handler(ServiceEvent("parse_start", provider=self.provider, provider_label=self.label))
        raise NotImplementedError

    def login(self, url: str, event_handler: ServiceEventHandler | None = None, **kwargs) -> str | None:
        if event_handler:
            event_handler(ServiceEvent("checking", provider=self.provider, provider_label=self.label))
            event_handler(ServiceEvent("waiting", provider=self.provider, provider_label=self.label))
        return None

    def upload_file(self, schema: FormSchema, question: Question, path: Path) -> Any:
        raise NotImplementedError

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
                    "dry_run_start" if dry_run else "fill_start",
                    provider=self.provider,
                    provider_label=self.label,
                )
            )
        return {"ok": True}

    def verify_submission(self, schema: FormSchema, answer_key: str) -> dict[str, Any]:
        return {}
```

常用事件：

- `parse_start`：开始解析表单。
- `parse_retry_start`：登录或验证后重新解析。
- `login_required`：需要登录。
- `checking`：检查登录态。
- `challenge_fetching`：正在获取二维码/验证码。
- `challenge`：返回 `image_bytes` 给 UI 显示。
- `waiting`：等待用户完成登录。
- `success`：登录成功，可带 `login_name`。
- `dry_run_start`：开始 dry-run 校验。
- `fill_start`：开始填写或提交目标平台。
