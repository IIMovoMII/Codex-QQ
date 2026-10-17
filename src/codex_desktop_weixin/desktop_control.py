"""按需连接现有 Desktop；基础入口保留原生“用户停止”逻辑。

当前 Windows Desktop 使用带长度头的本地命名管道。这里既不启动另一个
app-server，也不改历史文件。明确的控制指令才建立短连接，扩展入口在各自模块校验。
协议版本不匹配、窗口未拥有该对话、超时等情况一律返回失败，不猜其他目标。
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import time
import uuid
from ctypes import wintypes
from typing import Any


PIPE_PATH = r"\\.\pipe\codex-ipc"
MAX_FRAME_BYTES = 8 * 1024 * 1024
STOP_METHOD = "thread-follower-interrupt-turn"
# 本机 26.1007.2314.0：无 expectedTurnId 的本地 user-stop 请求使用版本 3。
# 它会暂停原生目标、清理正在运行的执行，并处理子代理，和 Desktop 按钮同路。
STOP_VERSION = 3


class DesktopControlError(Exception):
    pass


def encode_frame(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise DesktopControlError("frame-too-large")
    return struct.pack("<I", len(payload)) + payload


class WindowsDesktopPipe:
    """短连接，有界等待；不在后台持续轮询。"""

    def __init__(self, timeout_seconds: float = 12.0, *, path: str | None = None) -> None:
        if os.name != "nt":
            raise DesktopControlError("unsupported-platform")
        import msvcrt

        self.deadline = time.monotonic() + timeout_seconds
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.peek = self.kernel32.PeekNamedPipe
        self.peek.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.peek.restype = wintypes.BOOL
        self.stream = open(path or PIPE_PATH, "r+b", buffering=0)
        self.handle = msvcrt.get_osfhandle(self.stream.fileno())

    def __enter__(self) -> WindowsDesktopPipe:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.stream.close()

    def write(self, message: dict[str, Any]) -> None:
        data = encode_frame(message)
        while data:
            if time.monotonic() >= self.deadline:
                raise TimeoutError
            written = self.stream.write(data)
            if not written:
                raise DesktopControlError("connection-closed")
            data = data[written:]

    def _read_exact(self, length: int) -> bytes:
        parts: list[bytes] = []
        while length:
            if time.monotonic() >= self.deadline:
                raise TimeoutError
            available = wintypes.DWORD()
            if not self.peek(self.handle, None, 0, None, ctypes.byref(available), None):
                raise OSError(ctypes.get_last_error(), "Desktop pipe closed")
            if not available.value:
                time.sleep(0.02)
                continue
            data = self.stream.read(min(length, available.value))
            if not data:
                raise DesktopControlError("connection-closed")
            parts.append(data)
            length -= len(data)
        return b"".join(parts)

    def read(self) -> dict[str, Any]:
        length = struct.unpack("<I", self._read_exact(4))[0]
        if not 0 < length <= MAX_FRAME_BYTES:
            raise DesktopControlError("frame-too-large")
        message = json.loads(self._read_exact(length).decode("utf-8", errors="strict"))
        if not isinstance(message, dict):
            raise DesktopControlError("invalid-response")
        return message


def pipe_server_identity(pipe: Any) -> tuple[str, float, str]:
    """Identity of the actual pipe server, not the process that supplied its path."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_pid = kernel.GetNamedPipeServerProcessId
    get_pid.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    get_pid.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not get_pid(pipe.handle, ctypes.byref(pid)):
        raise DesktopControlError("server-identity-unavailable")
    from launcher import _query_process_identity
    value = _query_process_identity(pid.value)
    image, created = value.get("process_image"), value.get("process_start_time")
    if not image or not created:
        raise DesktopControlError("server-identity-unavailable")
    return f"{pid.value}:{image}:{created}", float(created), image


class DesktopClient:
    def __init__(self, pipe: Any) -> None:
        self.pipe = pipe
        self.client_id = "initializing-client"
        self.broadcasts: dict[str, dict[str, Any]] = {}

    def request(
        self, method: str, params: dict[str, Any], *, version: int = 0,
        target_client_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        message = {
            "type": "request", "requestId": request_id,
            "sourceClientId": self.client_id, "version": version,
            "method": method, "params": params, "timeoutMs": 10000,
        }
        if target_client_id is not None:
            message["targetClientId"] = target_client_id
        self.pipe.write(message)
        while True:
            response = self.pipe.read()
            if response.get("type") == "client-discovery-request":
                # 本客户端不拥有任何 Desktop 对话，不能让其他窗口把任务交给它。
                self.pipe.write({
                    "type": "client-discovery-response", "requestId": response.get("requestId"),
                    "response": {"canHandle": False},
                })
                continue
            if response.get("type") == "broadcast":
                method_name = response.get("method")
                if method_name in {"thread-stream-state-changed", "thread-queued-followups-changed"}:
                    self.broadcasts[str(method_name)] = response
                continue
            if response.get("type") != "response" or response.get("requestId") != request_id:
                continue
            if response.get("resultType") != "success":
                raise DesktopControlError(str(response.get("error") or "invalid-response"))
            if response.get("method") != method or not isinstance(response.get("result"), dict):
                raise DesktopControlError("invalid-response")
            return response

    def initialize(self) -> None:
        response = self.request("initialize", {"clientType": "codex-desktop-qq-bridge"})
        client_id = response["result"].get("clientId")
        if not isinstance(client_id, str) or not client_id:
            raise DesktopControlError("invalid-response")
        self.client_id = client_id

    def following(self, thread_id: str, following: bool, owner_id: str) -> None:
        self.pipe.write({
            "type": "broadcast", "sourceClientId": self.client_id, "version": 1,
            "method": "thread-stream-following-changed", "targetClientIds": [owner_id],
            "params": {"hostId": "local", "conversationId": thread_id, "following": following},
        })

    def owner(self, thread_id: str) -> str:
        response = self.request(
            "thread-owner-discovery", {"hostId": "local", "conversationId": thread_id}, version=1,
        )
        owner_id = response.get("handledByClientId")
        if not isinstance(owner_id, str) or not owner_id:
            raise DesktopControlError("invalid-response")
        return owner_id

    def stop(self, thread_id: str, owner_id: str) -> dict[str, Any]:
        response = self.request(
            STOP_METHOD, {"conversationId": thread_id, "mode": "user-stop"},
            version=STOP_VERSION, target_client_id=owner_id,
        )
        if response.get("handledByClientId") != owner_id:
            raise DesktopControlError("invalid-response")
        return response["result"]


def pause_desktop(thread_id: str, *, pipe_factory: Any = WindowsDesktopPipe) -> tuple[bool, str]:
    # 不依赖 qq_desktop_bridge，避免入口互相导入；和路由使用同样的编号范围。
    import re

    if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", thread_id):
        return False, "对话编号格式不正确。"
    sent = False
    try:
        with pipe_factory() as pipe:
            client = DesktopClient(pipe)
            client.initialize()
            owner_id = client.owner(thread_id)
            sent = True
            result = client.stop(thread_id, owner_id)
            if result.get("ok") is not True or "interruptedTurnId" not in result:
                raise DesktopControlError("invalid-response")
            turn_id = result["interruptedTurnId"]
            if turn_id is None:
                return True, "该对话当前没有正在运行的轮次；原生停止检查已完成。"
            if not isinstance(turn_id, str) or not turn_id:
                raise DesktopControlError("invalid-response")
            if result.get("goalPauseError"):
                return True, "本轮已停止，但自动任务目标暂停失败；请在 Desktop 检查，可能还会自动续跑。"
            return True, "已通过 Desktop 原生停止功能暂停本轮；可发送 /continue 继续。"
    except (TimeoutError, OSError) as error:
        if sent:
            return False, "暂停请求未取得确认，可能已生效；请查看 Desktop。不会自动重试或发送“暂停”消息。"
        if isinstance(error, TimeoutError):
            return False, "连接 Desktop 超时，未发送暂停请求。"
        return False, "无法连接正在运行的 Desktop，请确认它已打开。"
    except (DesktopControlError, ValueError, UnicodeError) as error:
        if str(error) == "no-client-found":
            if sent:
                return False, "暂停接口不可用：窗口状态或接口版本可能已变化，请在 Desktop 检查。"
            return False, "该对话当前未被 Desktop 窗口载入，请先在 Desktop 打开它。"
        if str(error) == "request-version-mismatch":
            return False, "Desktop 停止接口版本已变化，暂停未执行，需要更新桥接适配。"
        if sent:
            return False, "未取得有效的暂停确认，请查看 Desktop；不会自动重试。"
        return False, "Desktop 本地接口返回了不兼容的数据，未发送暂停请求。"
