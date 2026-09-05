# 更新日志

## v1.4 - 2026-09-04

- **迁移至 AstrBot master (v4.18+) 插件接口**：移除已废弃的 `@register(...)` 装饰器，name / author / desc / version 全部由 `metadata.yaml` 提供。
- **采用新 `astrbot.api.star` 命名空间**：`from astrbot.api.star import Context, Star` → `from astrbot.api import star`，引用改为 `star.Context` / `star.Star`，基类签名 `class FileSenderPlugin(star.Star)`。
- **`metadata.yaml` 补全字段**：新增 `astrbot_version` (PEP 440 范围约束 `>=4.17.0,<5`)、`support_platforms`、`keywords` (file-sender / onebot / napcat / cross-device / self-signed-cert)，与新版本 WebUI 元数据采集对齐。
- **`FunctionTool` 写法保持兼容**：通过 `@dataclass` 子类化 `FunctionTool`，字段 `name` / `description` / `parameters` 不变（继承自 `ToolSchema`，pydantic 校验）；`call()` 第一个参数仍叫 `context`，内部启发式从该参数中抽取 `event`。
- **不破坏兼容**：所有命令、事件、`on_llm_request` 钩子、LLM Tool 行为和 v1.3 100% 一致——直接覆盖安装即可。
- **保留 OneBot / NapCat 跨设备文件上传**：HTTPS 自签名证书通过 `_conf_schema.json` 中 `verify_ssl` 开关控制；命令名 `sf` / `/file` / LLM Tool `send_local_file` 全部保留。
- **`core/` / `tests/` / `requirements.txt` 等资产原样保留**：插件核心逻辑、`data/` 目录布局、Plugin Page 等未触动。
- **`main.py` 顶部新增「迁移要点」docstring**：记录与历史版本的关键差异，方便后续维护。

## v1.3

- 基础功能：手动命令 `sf` / `/file`，LLM Tool `send_local_file`，OneBot / NapCat 跨设备文件上传。
- 安全：NapCat HTTPS 自签名证书开关。