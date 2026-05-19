import asyncio
import base64
import hashlib
import json
import re
import shlex
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import AstrBotConfig, FunctionTool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_workspaces_path


@dataclass
class FileSenderTool(FunctionTool):
    name: str = "send_local_file"
    description: str = "发送或上传 AstrBot 本地文件。LLM 已经用 astrbot_file_write_tool 写出文件后，必须用本工具发送本地文件，不要用 send_message_to_user 的 file 类型。支持私聊、群文件和 QQ 群临时文件目标。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "要发送的本地文件绝对路径或相对路径。"},
                "file_name": {"type": "string", "description": "可选，自定义发送文件名。"},
                "target_type": {"type": "string", "description": "发送目标类型：auto、private、group、temp。temp 表示 QQ 群临时文件，按群文件接口发送到目标群。"},
                "target_user_id": {"type": "string", "description": "可选，指定 OneBot 用户 ID。"},
                "target_group_id": {"type": "string", "description": "可选，指定 OneBot 群号。"},
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
            return "当前 LLM 工具上下文无法获取会话事件，请改用 /sendfile 指令手动发送。"
        file_path = plugin._clean_option(kwargs.get("file_path"))
        file_name = plugin._clean_option(kwargs.get("file_name"))
        target_type = plugin._normalize_target_type(kwargs.get("target_type"))
        target_user_id = plugin._clean_option(kwargs.get("target_user_id"))
        target_group_id = plugin._clean_option(kwargs.get("target_group_id"))
        if not file_path:
            return "请提供要发送的文件路径。"
        try:
            await plugin.send_file(event.unified_msg_origin, file_path, file_name, target_type, target_user_id, target_group_id, event=event)
        except Exception as exc:
            return f"文件发送失败：{exc}"
        return "文件已发送。"


@dataclass
class UploadFileToUserTool(FileSenderTool):
    name: str = "upload_file_to_user"
    description: str = "上传/发送本地文件给当前用户或指定会话。用途等同 send_local_file；当路径来自 /AstrBot/data/workspaces 或 astrbot_file_write_tool 输出时，优先调用本工具。"


@register("astrbot_plugin_file_sender", "唐格天", "独立本地文件发送插件，支持 OneBot/NapCat 跨设备文件发送", "1.0.0")
class FileSenderPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.plugin_dir = Path(__file__).resolve().parent
        self.llm_tool = FileSenderTool()
        self.upload_tool = UploadFileToUserTool()
        self.llm_tool.plugin = self
        self.upload_tool.plugin = self
        if self.config.get("enable_llm_tool", False):
            context.add_llm_tools(self.llm_tool, self.upload_tool)

    @filter.on_llm_request()
    async def guide_file_tool_usage(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
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
            yield event.plain_result("用法：/sendfile <文件路径> [文件名] [target_type] [target_user_id] [target_group_id]。target_type 可选 auto/private/group/temp。")
            return
        file_path = args[0]
        file_name = args[1] if len(args) >= 2 else ""
        target_type = self._normalize_target_type(args[2] if len(args) >= 3 else "auto")
        target_user_id = args[3] if len(args) >= 4 else ""
        target_group_id = args[4] if len(args) >= 5 else ""
        try:
            await self.send_file(event.unified_msg_origin, file_path, file_name, target_type, target_user_id, target_group_id, event=event)
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
            raise RuntimeError("File Sender 仅支持 OneBot/aiocqhttp 适配器，请不要在非 OneBot 会话中调用此插件。")
        path = self._resolve_file_path(file_path, unified_msg_origin)
        target_type = self._normalize_target_type(target_type)
        target_user_id = self._clean_option(target_user_id)
        target_group_id = self._clean_option(target_group_id)
        display_name = file_name or path.name
        await asyncio.to_thread(self._send_cross_device_file, unified_msg_origin, path, display_name, target_type, target_user_id, target_group_id)

    def _send_cross_device_file(
        self,
        unified_msg_origin: str,
        path: Path,
        file_name: str,
        target_type: str,
        target_user_id: str,
        target_group_id: str,
    ) -> None:
        api_base = self._clean_option(self.config.get("onebot_cross_device_api_base_url"))
        if not api_base:
            raise RuntimeError("已开启跨设备 OneBot 上传，但未配置 onebot_cross_device_api_base_url。")
        api_base = api_base.rstrip("/")
        uploaded_path = self._upload_file_stream_to_onebot(api_base, path)
        resolved_target = self._resolve_onebot_target(unified_msg_origin, target_type, target_user_id, target_group_id)
        if resolved_target[0] == "group":
            self._upload_group_file_to_onebot(api_base, resolved_target[1], uploaded_path, file_name)
        else:
            self._upload_private_file_to_onebot(api_base, resolved_target[1], uploaded_path, file_name)

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
        completion = self._post_onebot_stream_payload(api_base, headers, {"stream_id": stream_id, "is_complete": True})
        response_data = completion.get("data") if isinstance(completion, dict) else None
        if not isinstance(response_data, dict) or response_data.get("status") != "file_complete":
            raise RuntimeError(f"NapCat upload_file_stream 合并结果异常：{completion}")
        uploaded_path = str(response_data.get("file_path") or "").strip()
        if not uploaded_path:
            raise RuntimeError(f"NapCat upload_file_stream 未返回 file_path：{completion}")
        return uploaded_path

    def _post_onebot_stream_payload(self, api_base: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            url=f"{api_base}/upload_file_stream",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response_payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"NapCat upload_file_stream 请求失败：HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"NapCat upload_file_stream 网络错误：{exc.reason}") from exc
        if response_payload:
            try:
                parsed = json.loads(response_payload)
            except json.JSONDecodeError:
                return {"raw": response_payload}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _upload_private_file_to_onebot(self, api_base: str, user_id: str, file_path: str, file_name: str) -> None:
        if not user_id:
            raise RuntimeError("无法确定 OneBot 用户 ID。")
        self._post_onebot_json(
            api_base,
            "upload_private_file",
            {"user_id": user_id, "file": file_path, "name": file_name, "upload_file": True},
        )

    def _upload_group_file_to_onebot(self, api_base: str, group_id: str, file_path: str, file_name: str) -> None:
        if not group_id:
            raise RuntimeError("无法确定 OneBot 群号。")
        self._post_onebot_json(api_base, "upload_group_file", {"group_id": group_id, "file": file_path, "name": file_name, "folder": "/"})

    def _post_onebot_json(self, api_base: str, action: str, payload: dict[str, object]) -> None:
        request = urllib.request.Request(
            url=f"{api_base}/{action}",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._onebot_headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"NapCat {action} 请求失败：HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"NapCat {action} 网络错误：{exc.reason}") from exc

    def _resolve_file_path(self, value: str, unified_msg_origin: str = "") -> Path:
        raw = self._clean_option(value)
        if not raw:
            raise RuntimeError("文件路径不能为空。")
        path = Path(raw)
        candidates = [path]
        if not path.is_absolute():
            workspace_path = self._workspace_file_path(unified_msg_origin, path)
            candidates = [workspace_path, (Path.cwd() / path).resolve(), (self.plugin_dir / path).resolve()]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        raise RuntimeError(f"文件不存在：{candidates[0]}")

    def _workspace_file_path(self, unified_msg_origin: str, path: Path) -> Path:
        normalized_umo = re.sub(r"[^A-Za-z0-9._-]+", "_", str(unified_msg_origin or "").strip()) or "unknown"
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

    def _resolve_onebot_target(
        self,
        unified_msg_origin: str,
        target_type: str,
        target_user_id: str,
        target_group_id: str,
    ) -> tuple[str, str]:
        if target_type in {"group", "temp"}:
            group_id = target_group_id or self._extract_onebot_group_id(unified_msg_origin)
            if not group_id:
                raise RuntimeError("target_type=group/temp 时需要提供 target_group_id，或在群聊中调用。")
            return "group", group_id
        if target_type == "private":
            user_id = target_user_id or self._extract_onebot_user_id(unified_msg_origin)
            if not user_id:
                raise RuntimeError("target_type=private 时需要提供 target_user_id，或在私聊中调用。")
            return "private", user_id
        if self._is_onebot_group_origin(unified_msg_origin):
            return "group", self._extract_onebot_group_id(unified_msg_origin)
        return "private", target_user_id or self._extract_onebot_user_id(unified_msg_origin)

    def _extract_onebot_user_id(self, unified_msg_origin: str) -> str:
        origin = str(unified_msg_origin or "")
        for part in reversed([item for item in origin.replace("/", ":").split(":") if item]):
            if part.isdigit():
                return part
        return ""

    def _extract_onebot_group_id(self, unified_msg_origin: str) -> str:
        origin = str(unified_msg_origin or "")
        parts = [item for item in origin.replace("/", ":").split(":") if item]
        lowered = [item.lower() for item in parts]
        for index, part in enumerate(lowered):
            if part == "group" and index + 1 < len(parts) and parts[index + 1].isdigit():
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
