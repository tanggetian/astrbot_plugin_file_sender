"""astrbot_plugin_file_sender (新版本 AstrBot API 适配版)

迁移要点（与历史版本的差异）：
- 移除已废弃的 ``@register`` 装饰器，name/author/desc/version 由 metadata.yaml 提供。
- 将 ``from astrbot.api.star import Context, Star`` 替换为新的
  ``from astrbot.api import star`` 风格，并通过 ``star.Context``/``star.Star`` 引用。
- ``FunctionTool`` 的 dataclass 字段保持 ``name``/``description``/``parameters`` 不变
  （继承自 ``ToolSchema``，由 pydantic 校验），``call()`` 方法的第一个参数名仍叫
  ``context``，内部沿用原启发式逻辑从该参数中抽取 ``event``。
- v1.4.1 起改用 ``pydantic.dataclasses.dataclass`` 装饰 ``FileSenderTool`` / ``UploadFileToUserTool``，
  并把 ``field(default_factory=...)`` 用 ``pydantic.Field as field`` 别名接入，
  修复父类 ``ToolSchema.validate_parameters`` 在子类实例化时丢失校验器的问题（之前用标准库
  ``@dataclass`` 会让 ``__pydantic_validator__`` 在子类被擦除，构造阶段不校验
  ``parameters`` JSON Schema，仅靠 FunctionToolManager 运行时再校验一次——pydantic dataclass
  写法可在构造阶段就拦下非法 schema）。
- 其余命令/事件/LLM request 钩子全部保持兼容写法。
"""

import asyncio
import base64
import hashlib
import json
import re
import shlex
import ssl
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from pydantic import Field as field
from pydantic.dataclasses import dataclass

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.log import LogManager
from astrbot.core.utils.astrbot_path import get_astrbot_workspaces_path


@dataclass
class FileSenderTool(FunctionTool):
    name: str = "send_local_file"
    description: str = "发送或上传 AstrBot 本地文件。LLM 已经用 astrbot_file_write_tool 写出文件后，必须用本工具发送本地文件，不要用 send_message_to_user 的 file 类型。支持私聊、群文件和 QQ 群临时文件目标。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "要发送的本地文件绝对路径或相对路径。",
                },
                "file_name": {
                    "type": "string",
                    "description": "可选，自定义发送文件名。",
                },
                "target_type": {
                    "type": "string",
                    "description": "发送目标类型：auto、private、group、temp。temp 表示 QQ 群临时文件，按群文件接口发送到目标群。",
                },
                "target_user_id": {
                    "type": "string",
                    "description": "可选，指定 OneBot 用户 ID。",
                },
                "target_group_id": {
                    "type": "string",
                    "description": "可选，指定 OneBot 群号。",
                },
            },
            "required": ["file_path"],
        }
    )

    async def call(self, context, **kwargs):
        plugin = getattr(self, "plugin", None)
        if plugin is None:
            return "文件发送插件尚未初始化。"
        event = plugin._get_event_from_tool_context(context)
        if event is None:
            return (
                "当前 LLM 工具上下文无法获取会话事件，请改用 /sendfile 指令手动发送。"
            )
        file_path = plugin._clean_option(kwargs.get("file_path"))
        file_name = plugin._clean_option(kwargs.get("file_name"))
        target_type = plugin._normalize_target_type(kwargs.get("target_type"))
        target_user_id = plugin._clean_option(kwargs.get("target_user_id"))
        target_group_id = plugin._clean_option(kwargs.get("target_group_id"))
        if not file_path:
            return "请提供要发送的文件路径。"
        try:
            await plugin.send_file(
                event.unified_msg_origin,
                file_path,
                file_name,
                target_type,
                target_user_id,
                target_group_id,
                event=event,
            )
        except Exception as exc:
            return f"文件发送失败：{exc}"
        return "文件已发送。"


@dataclass
class UploadFileToUserTool(FileSenderTool):
    name: str = "upload_file_to_user"
    description: str = "上传/发送本地文件给当前用户或指定会话。用途等同 send_local_file；当路径来自 /AstrBot/data/workspaces 或 astrbot_file_write_tool 输出时，优先调用本工具。"


# 注意：新版本 AstrBot 不再需要 @register(...) 装饰器。
# 插件的 name/author/desc/version 由 metadata.yaml 提供，类继承自 star.Star 即被自动识别。
class FileSenderPlugin(star.Star):
    """File Sender 插件主类（基于新版本 AstrBot API）。

    元数据（name/author/desc/version/repo）见 metadata.yaml；
    通过 ``super().__init__(context)`` 让基类完成 self.context / self.logger 的初始化，
    本插件自行持有 config（``self.config = config``）。
    """

    def __init__(self, context: star.Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config
        self.plugin_dir = Path(__file__).resolve().parent
        self.logger = LogManager.GetLogger("astrbot.plugin.file_sender")
        self._ssl_context: ssl.SSLContext | None = None
        self._ssl_context_signature: tuple | None = None
        self.llm_tool = FileSenderTool()
        self.upload_tool = UploadFileToUserTool()
        self.llm_tool.plugin = self
        self.upload_tool.plugin = self
        self._install_context_send_interceptor()
        if self.config.get("enable_llm_tool", False):
            context.add_llm_tools(self.llm_tool, self.upload_tool)

    def _install_context_send_interceptor(self) -> None:
        self.context._file_sender_plugin = self
        if getattr(self.context, "_file_sender_send_message_patched", False):
            return
        original_send_message = self.context.send_message
        self.context._file_sender_original_send_message = original_send_message

        async def send_message_with_file_sender(session, message_chain):
            plugin = getattr(self.context, "_file_sender_plugin", None)
            if plugin is None:
                return await original_send_message(session, message_chain)
            return await plugin._send_message_with_file_sender(
                original_send_message, session, message_chain
            )

        self.context.send_message = send_message_with_file_sender
        self.context._file_sender_send_message_patched = True

    async def terminate(self) -> None:
        original_send_message = getattr(
            self.context, "_file_sender_original_send_message", None
        )
        if original_send_message is not None:
            self.context.send_message = original_send_message
        for attr in (
            "_file_sender_plugin",
            "_file_sender_original_send_message",
            "_file_sender_send_message_patched",
        ):
            if hasattr(self.context, attr):
                delattr(self.context, attr)

    async def _send_message_with_file_sender(
        self, original_send_message, session, message_chain
    ):
        unified_msg_origin = str(session)
        if not self._is_onebot_session(session, unified_msg_origin):
            return await original_send_message(session, message_chain)
        if not getattr(message_chain, "chain", None):
            return await original_send_message(session, message_chain)
        handled_chain = await self._handle_file_components(
            unified_msg_origin, message_chain.chain, skip_context_check=True
        )
        if handled_chain is message_chain.chain:
            return await original_send_message(session, message_chain)
        if not handled_chain:
            return True
        if hasattr(message_chain, "derive"):
            message_chain = message_chain.derive(handled_chain)
        else:
            message_chain.chain = handled_chain
        return await original_send_message(session, message_chain)

    @filter.on_decorating_result()
    async def intercept_onebot_file_result(self, event: AstrMessageEvent) -> None:
        if not self._is_onebot_context(event, getattr(event, "unified_msg_origin", "")):
            return
        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        handled_chain = await self._handle_file_components(
            event.unified_msg_origin, result.chain, event=event
        )
        if handled_chain is not result.chain:
            result.chain = handled_chain

    async def _handle_file_components(
        self,
        unified_msg_origin: str,
        chain: list,
        event=None,
        skip_context_check: bool = False,
    ) -> list:
        kept_chain = []
        changed = False
        for component in chain:
            if not isinstance(component, File):
                kept_chain.append(component)
                continue
            file_path = self._component_file_path(component)
            if not file_path:
                kept_chain.append(component)
                continue
            try:
                await self._send_file_resolved_context(
                    unified_msg_origin,
                    file_path,
                    self._clean_option(getattr(component, "name", "")),
                    event=event,
                    skip_context_check=skip_context_check,
                )
            except Exception as exc:
                kept_chain.append(Plain(f"文件转发失败：{exc}"))
            changed = True
        if changed:
            return kept_chain
        return chain

    async def _send_file_resolved_context(
        self,
        unified_msg_origin: str,
        file_path: str,
        file_name: str = "",
        event=None,
        skip_context_check: bool = False,
    ) -> None:
        if not skip_context_check:
            await self.send_file(
                unified_msg_origin,
                file_path,
                file_name,
                event=event,
            )
            return
        path = self._resolve_file_path(file_path, unified_msg_origin)
        display_name = file_name or path.name
        await asyncio.to_thread(
            self._send_cross_device_file,
            unified_msg_origin,
            path,
            display_name,
            "auto",
            "",
            "",
        )

    @filter.on_llm_request()
    async def guide_file_tool_usage(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self.config.get("enable_llm_tool", False):
            return
        if not self._is_onebot_context(event, getattr(event, "unified_msg_origin", "")):
            return
        guide = (
            "\n# File Sending Rule\n"
            "When you need to send a local file path to the user, especially a file created by astrbot_file_write_tool "
            "or a path under /AstrBot/data/workspaces, call upload_file_to_user or send_local_file. "
            "If astrbot_file_write_tool returns a relative path such as test.txt, pass that relative path directly; File Sender resolves it from the current AstrBot workspace. "
            "Do not use send_message_to_user with a file component for local files, because the OneBot/NapCat side may not share AstrBot's filesystem and can raise ENOENT.\n"
        )
        req.system_prompt = f"{req.system_prompt or ''}{guide}"

    @filter.command("sendfile")
    async def sendfile(self, event: AstrMessageEvent):
        args = self._parse_args(event.message_str)
        if not args:
            yield event.plain_result(
                "用法：/sendfile <文件路径> [文件名] [target_type] [target_user_id] [target_group_id]。target_type 可选 auto/private/group/temp。"
            )
            return
        file_path = args[0]
        file_name = args[1] if len(args) >= 2 else ""
        target_type = self._normalize_target_type(args[2] if len(args) >= 3 else "auto")
        target_user_id = args[3] if len(args) >= 4 else ""
        target_group_id = args[4] if len(args) >= 5 else ""
        try:
            await self.send_file(
                event.unified_msg_origin,
                file_path,
                file_name,
                target_type,
                target_user_id,
                target_group_id,
                event=event,
            )
        except Exception as exc:
            yield event.plain_result(f"文件发送失败：{exc}")
            return
        yield event.plain_result("文件已发送。")

    async def send_file(
        self,
        unified_msg_origin: str,
        file_path: str,
        file_name: str = "",
        target_type: str = "auto",
        target_user_id: str = "",
        target_group_id: str = "",
        event=None,
    ) -> None:
        if not self._is_onebot_context(event, unified_msg_origin):
            raise RuntimeError(
                "File Sender 仅支持 OneBot/aiocqhttp 适配器，请不要在非 OneBot 会话中调用此插件。"
            )
        path = self._resolve_file_path(file_path, unified_msg_origin)
        target_type = self._normalize_target_type(target_type)
        target_user_id = self._clean_option(target_user_id)
        target_group_id = self._clean_option(target_group_id)
        display_name = file_name or path.name
        await asyncio.to_thread(
            self._send_cross_device_file,
            unified_msg_origin,
            path,
            display_name,
            target_type,
            target_user_id,
            target_group_id,
        )

    def _send_cross_device_file(
        self,
        unified_msg_origin: str,
        path: Path,
        file_name: str,
        target_type: str,
        target_user_id: str,
        target_group_id: str,
    ) -> None:
        api_base = self._clean_option(
            self.config.get("onebot_cross_device_api_base_url")
        )
        if not api_base:
            raise RuntimeError(
                "已开启跨设备 OneBot 上传，但未配置 onebot_cross_device_api_base_url。"
            )
        api_base = api_base.rstrip("/")
        uploaded_path = self._upload_file_stream_to_onebot(api_base, path)
        resolved_target = self._resolve_onebot_target(
            unified_msg_origin, target_type, target_user_id, target_group_id
        )
        if resolved_target[0] == "group":
            self._upload_group_file_to_onebot(
                api_base, resolved_target[1], uploaded_path, file_name
            )
        else:
            self._upload_private_file_to_onebot(
                api_base, resolved_target[1], uploaded_path, file_name
            )

    def _upload_file_stream_to_onebot(self, api_base: str, path: Path) -> str:
        file_bytes = path.read_bytes()
        file_size = len(file_bytes)
        stream_id = f"file-sender-{uuid.uuid4().hex}"
        chunk_size = self._onebot_stream_chunk_size()
        total_chunks = max(1, (file_size + chunk_size - 1) // chunk_size)
        expected_sha256 = hashlib.sha256(file_bytes).hexdigest()
        headers = self._onebot_headers()
        for chunk_index in range(total_chunks):
            start = chunk_index * chunk_size
            end = min(start + chunk_size, file_size)
            payload = {
                "stream_id": stream_id,
                "chunk_data": base64.b64encode(file_bytes[start:end]).decode("ascii"),
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "file_size": file_size,
                "expected_sha256": expected_sha256,
                "filename": path.name,
            }
            self._post_onebot_stream_payload(api_base, headers, payload)
        completion = self._post_onebot_stream_payload(
            api_base, headers, {"stream_id": stream_id, "is_complete": True}
        )
        response_data = completion.get("data") if isinstance(completion, dict) else None
        if (
            not isinstance(response_data, dict)
            or response_data.get("status") != "file_complete"
        ):
            raise RuntimeError(f"NapCat upload_file_stream 合并结果异常：{completion}")
        uploaded_path = str(response_data.get("file_path") or "").strip()
        if not uploaded_path:
            raise RuntimeError(
                f"NapCat upload_file_stream 未返回 file_path：{completion}"
            )
        return uploaded_path

    def _post_onebot_stream_payload(
        self, api_base: str, headers: dict[str, str], payload: dict[str, object]
    ) -> dict[str, object]:
        request = urllib.request.Request(
            url=f"{api_base}/upload_file_stream",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self._open_onebot_url(request, timeout=60) as response:
                response_payload = response.read().decode("utf-8", errors="replace")
        except RuntimeError as exc:
            raise RuntimeError(f"NapCat upload_file_stream 请求失败：{exc}") from exc
        if response_payload:
            try:
                parsed = json.loads(response_payload)
            except json.JSONDecodeError:
                return {"raw": response_payload}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _upload_private_file_to_onebot(
        self, api_base: str, user_id: str, file_path: str, file_name: str
    ) -> None:
        if not user_id:
            raise RuntimeError("无法确定 OneBot 用户 ID。")
        self._post_onebot_json(
            api_base,
            "upload_private_file",
            {
                "user_id": user_id,
                "file": file_path,
                "name": file_name,
                "upload_file": True,
            },
        )

    def _upload_group_file_to_onebot(
        self, api_base: str, group_id: str, file_path: str, file_name: str
    ) -> None:
        if not group_id:
            raise RuntimeError("无法确定 OneBot 群号。")
        self._post_onebot_json(
            api_base,
            "upload_group_file",
            {"group_id": group_id, "file": file_path, "name": file_name, "folder": "/"},
        )

    def _post_onebot_json(
        self, api_base: str, action: str, payload: dict[str, object]
    ) -> None:
        request = urllib.request.Request(
            url=f"{api_base}/{action}",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._onebot_headers(),
            method="POST",
        )
        try:
            with self._open_onebot_url(request, timeout=60) as response:
                response.read()
        except RuntimeError as exc:
            raise RuntimeError(f"NapCat {action} 请求失败：{exc}") from exc

    def _resolve_file_path(self, value: str, unified_msg_origin: str = "") -> Path:
        raw = self._clean_option(value)
        if not raw:
            raise RuntimeError("文件路径不能为空。")
        path = Path(raw)
        candidates = [path]
        if not path.is_absolute():
            workspace_path = self._workspace_file_path(unified_msg_origin, path)
            candidates = [
                workspace_path,
                (Path.cwd() / path).resolve(),
                (self.plugin_dir / path).resolve(),
            ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        raise RuntimeError(f"文件不存在：{candidates[0]}")

    def _component_file_path(self, component: File) -> str:
        file_path = self._clean_option(getattr(component, "file_", ""))
        if file_path and not self._looks_like_url(file_path):
            return file_path
        return ""

    def _looks_like_url(self, value: str) -> bool:
        return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value or ""))

    def _workspace_file_path(self, unified_msg_origin: str, path: Path) -> Path:
        normalized_umo = (
            re.sub(r"[^A-Za-z0-9._-]+", "_", str(unified_msg_origin or "").strip())
            or "unknown"
        )
        return (Path(get_astrbot_workspaces_path()) / normalized_umo / path).resolve()

    def _onebot_stream_chunk_size(self) -> int:
        configured = self.config.get("onebot_cross_device_stream_chunk_size", 524288)
        try:
            value = int(configured)
        except (TypeError, ValueError):
            return 524288
        return max(65536, value)

    def _onebot_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self._clean_option(self.config.get("onebot_cross_device_api_token"))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _ssl_options(self) -> tuple[bool, str]:
        verify_raw = self.config.get("onebot_cross_device_ssl_verify", True)
        if isinstance(verify_raw, str):
            verify = verify_raw.strip().lower() not in {"false", "0", "no", "off"}
        else:
            verify = bool(verify_raw)
        ca_bundle = self._clean_option(
            self.config.get("onebot_cross_device_ssl_ca_bundle")
        )
        return verify, ca_bundle

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        verify, ca_bundle = self._ssl_options()
        signature = (verify, ca_bundle)
        if self._ssl_context is not None and self._ssl_context_signature == signature:
            return self._ssl_context
        if not verify:
            self.logger.warning(
                "NapCat HTTPS 证书校验已关闭（onebot_cross_device_ssl_verify=false），"
                "请勿在公网环境下使用。"
            )
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._ssl_context = context
            self._ssl_context_signature = signature
            return context
        context = ssl.create_default_context()
        if ca_bundle:
            ca_path = Path(ca_bundle).expanduser()
            if not ca_path.is_file():
                raise RuntimeError(f"配置的 NapCat CA 证书文件不存在：{ca_path}")
            try:
                context.load_verify_locations(cafile=str(ca_path))
            except ssl.SSLError as exc:
                raise RuntimeError(
                    f"加载 NapCat CA 证书失败：{exc}（请确保证书为 PEM 格式）"
                ) from exc
            self.logger.info("NapCat HTTPS 已加载自定义 CA 证书：%s", ca_path)
        self._ssl_context = context
        self._ssl_context_signature = signature
        return context

    def _open_onebot_url(self, request: urllib.request.Request, timeout: int = 60):
        ssl_context = self._build_ssl_context()
        try:
            return urllib.request.urlopen(request, timeout=timeout, context=ssl_context)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code} {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise RuntimeError(
                    f"NapCat HTTPS 证书校验失败：{reason}。"
                    "如使用自签名证书，请在插件配置中设置 "
                    "onebot_cross_device_ssl_ca_bundle 指向 PEM CA 文件，"
                    "或在可信内网环境下关闭 onebot_cross_device_ssl_verify。"
                ) from exc
            if isinstance(reason, ssl.SSLError):
                raise RuntimeError(f"NapCat HTTPS 错误：{reason}") from exc
            raise RuntimeError(f"NapCat 网络错误：{reason}") from exc

    def _resolve_onebot_target(
        self,
        unified_msg_origin: str,
        target_type: str,
        target_user_id: str,
        target_group_id: str,
    ) -> tuple[str, str]:
        if target_type in {"group", "temp"}:
            group_id = target_group_id or self._extract_onebot_group_id(
                unified_msg_origin
            )
            if not group_id:
                raise RuntimeError(
                    "target_type=group/temp 时需要提供 target_group_id，或在群聊中调用。"
                )
            return "group", group_id
        if target_type == "private":
            user_id = target_user_id or self._extract_onebot_user_id(unified_msg_origin)
            if not user_id:
                raise RuntimeError(
                    "target_type=private 时需要提供 target_user_id，或在私聊中调用。"
                )
            return "private", user_id
        if self._is_onebot_group_origin(unified_msg_origin):
            return "group", self._extract_onebot_group_id(unified_msg_origin)
        return "private", target_user_id or self._extract_onebot_user_id(
            unified_msg_origin
        )

    def _extract_onebot_user_id(self, unified_msg_origin: str) -> str:
        origin = str(unified_msg_origin or "")
        for part in reversed(
            [item for item in origin.replace("/", ":").split(":") if item]
        ):
            if part.isdigit():
                return part
        return ""

    def _extract_onebot_group_id(self, unified_msg_origin: str) -> str:
        origin = str(unified_msg_origin or "")
        parts = [item for item in origin.replace("/", ":").split(":") if item]
        lowered = [item.lower() for item in parts]
        for index, part in enumerate(lowered):
            if (
                part == "group"
                and index + 1 < len(parts)
                and parts[index + 1].isdigit()
            ):
                return parts[index + 1]
        for part in reversed(parts):
            if part.isdigit():
                return part
        return ""

    def _is_onebot_group_origin(self, unified_msg_origin: str) -> bool:
        origin = str(unified_msg_origin or "").lower().replace("/", ":")
        return ":group:" in origin or ":groupmessage:" in origin

    def _is_onebot_context(self, event, unified_msg_origin: str) -> bool:
        candidates = []
        if event is not None:
            getter = getattr(event, "get_platform_name", None)
            if callable(getter):
                try:
                    candidates.append(getter())
                except Exception:
                    pass
            platform_meta = getattr(event, "platform_meta", None)
            candidates.append(getattr(platform_meta, "name", ""))
            platform = getattr(event, "platform", None)
            candidates.append(getattr(platform, "name", ""))
            candidates.append(type(event).__name__)
            candidates.append(type(event).__module__)
        candidates.append(unified_msg_origin)
        return any(self._is_onebot_marker(candidate) for candidate in candidates)

    def _is_onebot_session(self, session, unified_msg_origin: str) -> bool:
        if self._is_onebot_context(None, unified_msg_origin):
            return True
        platform_name = self._session_platform_name(session, unified_msg_origin)
        for platform in getattr(self.context.platform_manager, "platform_insts", []):
            meta_getter = getattr(platform, "meta", None)
            meta = meta_getter() if callable(meta_getter) else None
            if self._clean_option(getattr(meta, "id", "")) != platform_name:
                continue
            candidates = [
                getattr(meta, "id", ""),
                getattr(meta, "name", ""),
                getattr(meta, "type", ""),
                type(platform).__name__,
                type(platform).__module__,
            ]
            return any(self._is_onebot_marker(candidate) for candidate in candidates)
        return False

    def _session_platform_name(self, session, unified_msg_origin: str) -> str:
        platform_name = self._clean_option(getattr(session, "platform_name", ""))
        if platform_name:
            return platform_name
        return self._clean_option(unified_msg_origin.split(":", 1)[0])

    def _is_onebot_marker(self, value) -> bool:
        marker = str(value or "").lower()
        return "aiocqhttp" in marker or "onebot" in marker

    def _normalize_target_type(self, value) -> str:
        target_type = self._clean_option(value).lower()
        if target_type in {"private", "group", "temp"}:
            return target_type
        return "auto"

    def _parse_args(self, message: str) -> list[str]:
        message = message.strip()
        if message.startswith("/sendfile"):
            message = message[len("/sendfile") :].strip()
        elif message.startswith("sendfile"):
            message = message[len("sendfile") :].strip()
        try:
            return shlex.split(message)
        except ValueError:
            return message.split()

    def _get_event_from_tool_context(self, context):
        candidates = [context]
        for attr in ("context", "ctx", "agent_context", "astr_context"):
            value = getattr(context, attr, None)
            if value is not None:
                candidates.append(value)
        for candidate in candidates:
            for attr in ("event", "message_event", "curr_event", "current_event"):
                event = getattr(candidate, attr, None)
                if event is not None:
                    return event
        return None

    def _clean_option(self, value) -> str:
        if value is None:
            return ""
        return str(value).strip()
