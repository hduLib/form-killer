# form-killer

中文 CLI 问卷答题工具，通过表单 provider 插件支持不同平台，并使用 OpenAI 生成答案。

```powershell
uv run form-killer
```

直接运行 `uv run form-killer`，后续按 CLI 内部提示走即可：

1. CLI 会显示当前已注册的表单 provider 插件列表。
2. 输入任一受支持平台的页面 URL。
3. 工具输出解析好的表单结构。
4. 对姓名、ID、邮箱、电话、地址、文件上传等字段，工具会标为用户输入。
5. 选择本次使用的 OpenAI 模型，默认使用已保存模型或 `gpt-5.5`。
6. AI 生成格式化答案并输出预览。
7. 最终由用户确认提交。

## 本地缓存

本地配置和缓存默认保存在项目目录 `.form-killer/`：

- `.form-killer/config.toml`：OpenAI API Key、Base URL、模型和 provider 配置。
- `.form-killer/sessions/`：浏览器登录 session。

OpenAI 配置优先级：

1. 命令行参数：`--api-key`、`--base-url`
2. 环境变量：`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`
3. 本地配置：`form-killer config set-openai-key`、`form-killer config set-openai-base-url`、`form-killer config set-openai-model`

## Dry-run

`--dry-run` 表示“预演/试跑”，用于检查解析、答案生成、必填项和提交数据格式，但不做最终提交。

```powershell
uv run form-killer answer "表单URL" --dry-run
```

行为说明：

- 会解析表单并生成答案预览。
- 会构造提交数据并尽量做校验。
- 不会向目标平台发送最终提交请求。
- 适合在首次使用新 provider、新表单或新答案文件时先跑一遍。

Provider 差异：

- Yandex Forms：使用平台接口的 `dryRun` 校验，不创建真实提交。
- 腾讯文档/WPS/金山表单：当前 dry-run 只做本地流程校验，不打开页面填写，也不点击提交。

真实提交仍需显式使用 `--submit`，并在交互确认后才会发送。

## Provider 插件开发

Provider 插件负责识别 URL、解析表单、上传文件、提交答案，以及可选的登录流程。CLI 不应写死某个平台；它只读取已注册 provider 列表，并监听服务事件来输出日志、二维码和状态。

### 注册方式

当前项目的第三方 provider 也是直接加在本仓库里，不需要另起一个 Python project。插件必须由插件模块自己注册。核心代码只做一件事：

1. 启动时加载内置 provider 模块。

不要把 provider 注册放进 `form_killer.forms.__init__`，也不要让核心路由 import 某个具体平台。

在本项目内新增 provider 的步骤：

1. 新建模块，例如 `src/form_killer/forms/example.py`。
2. 在模块里实现 service 和 provider 注册代码。
3. 把模块名加入 `src/form_killer/forms/services.py` 的 `BUILTIN_FORM_PROVIDER_MODULES`。
4. 增加对应测试。

最简单的插件入口是 `register_form_provider()`：

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

如果不是按域名匹配，也可以实现自定义 provider：

```python
from form_killer.forms.services import RoutedFormService, register_form_provider


class ExampleProvider:
    name = "example"
    label = "Example Forms"

    def resolve(self, url: str):
        if not url.startswith("example://"):
            return None
        return RoutedFormService(
            provider=self.name,
            document_type="form",
            service=ExampleFormService(document_type="form"),
        )


register_form_provider(ExampleProvider())
```

### 本项目内加载

本项目启动时会加载 `BUILTIN_FORM_PROVIDER_MODULES` 中列出的模块。新增仓库内 provider 时，把模块路径加进去即可：

```python
BUILTIN_FORM_PROVIDER_MODULES = (
    "form_killer.forms.yandex",
    "form_killer.forms.tencent",
    "form_killer.forms.wps",
    "form_killer.forms.example",
)
```

模块被加载后，模块内的 `register_form_provider(...)` 会执行。不需要额外入口函数，也不需要任何打包配置。

### 服务实现

服务类建议实现 `FormService` 协议：

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
        # TODO: 拉取页面或接口，返回 FormSchema
        raise NotImplementedError

    def login(self, url: str, event_handler: ServiceEventHandler | None = None, **kwargs) -> str | None:
        if event_handler:
            event_handler(ServiceEvent("checking", provider=self.provider, provider_label=self.label))
            # 如需二维码，发送 image_bytes，让 CLI 决定如何显示
            # event_handler(ServiceEvent("challenge", provider=self.provider, provider_label=self.label, image_bytes=png_bytes))
            event_handler(ServiceEvent("waiting", provider=self.provider, provider_label=self.label))
        # TODO: 完成登录并保存 session
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
        # TODO: dry-run 或真实提交
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
