"""最小的 QQ → Codex Desktop 桥接代理。

这个文件不是一个新的智能体。它实现 CC Connect 的 ACP 标准输入输出，
只负责这些本地控制：

* 现行 ControlRouter 要求逐条引用，空闲/暂停时走原生开始新一轮，
  运行中通过 ``codex queue --thread ...`` 排队；
* 从 QQ 的“引用提醒”中提取完整线程编号，再把用户的回复排入对应
  Codex Desktop 对话；不从旧选择猜测本条目标。
* 把明确的“暂停”指令交给拥有该对话的 Desktop 窗口，执行原生停止。

它不读取提示词历史、不猜测最近对话，也不执行 shell 命令。目标线程必须
由用户显式选择，线程编号会经过格式检查并作为独立的子进程参数传递。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from desktop_control import pause_desktop


# 旧版本曾把这个标记交给 CC Connect；保留解析只是为了兼容已经排队的旧消息。
# 当前出站提醒由 `cc-connect send` 直接发往 QQ，不再使用这个标记。
OUTBOUND_MARKER = "__CODEX_DESKTOP_NOTIFICATION_V1__\n"
MAX_MESSAGE_CHARS = 4000
THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
THREAD_REFERENCE_PATTERN = re.compile(
    r"(?:Codex\s*)?(?:对话编号|线程编号|线程)\s*[：:]\s*([A-Za-z0-9_-]{20,100})",
    re.IGNORECASE,
)


def _runtime_data_dir() -> Path:
    configured = os.environ.get("CC_DATA_DIR", "").strip()
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "CodexCC"
    return Path.home() / "AppData" / "Local" / "CodexCC"


def routes_path() -> Path:
    return _runtime_data_dir() / "qq-desktop-routes.json"


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_routes(path: Path | None = None) -> dict[str, str]:
    target = path or routes_path()
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(thread_id)
        for key, thread_id in value.items()
        if isinstance(key, str) and isinstance(thread_id, str) and valid_thread_id(thread_id)
    }


def save_routes(routes: dict[str, str], path: Path | None = None) -> None:
    target = path or routes_path()
    _atomic_write_json(target, routes)


def valid_thread_id(value: str) -> bool:
    return bool(THREAD_ID_PATTERN.fullmatch(value.strip()))


def extract_prompt_text(params: Any) -> str:
    """Extract text blocks from an ACP session/prompt payload."""
    if not isinstance(params, dict):
        return ""
    prompt = params.get("prompt")
    if isinstance(prompt, str):
        return prompt.strip()
    if not isinstance(prompt, list):
        return ""
    parts: list[str] = []
    for block in prompt:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts).strip()


def _thread_label(thread_id: str) -> str:
    return thread_id[-6:]


def extract_quoted_thread(text: str) -> tuple[str | None, str]:
    """Extract a thread ID from CC Connect's normalized QQ quote text.

    CC Connect currently normalizes a QQ quote to::

        [引用消息]\n<quoted message>\n\n<new reply>

    The quoted notification keeps ``对话编号：<full id>`` as its final line.
    We deliberately require the ``[引用消息]`` prefix so an arbitrary ID in a
    normal user message cannot silently change the route.  Returning the text
    after the ID also removes the quoted notification from the Desktop prompt.
    """
    normalized = text.strip()
    if not normalized.startswith("[引用消息]"):
        return None, normalized
    match = THREAD_REFERENCE_PATTERN.search(normalized)
    if match is None:
        return None, normalized
    # The notification's thread line is the last line of the quoted block.
    # Any remaining text is the new QQ reply; lstrip handles both the exact
    # ``\n\n`` separator and platform-specific extra line breaks.
    reply = normalized[match.end() :].lstrip()
    return match.group(1), reply


def parse_control_message(
    text: str,
    *,
    current_thread: str | None,
) -> tuple[str, str | None, str]:
    """Return ``(action, thread_id, payload)`` without touching the filesystem.

    Actions are ``reply`` (send a reply to QQ), ``queue`` (queue payload to
    Desktop), ``pause`` (native Desktop stop), and ``ignore`` (an internal outbound notification). Plain text is
    accepted only after an explicit ``/use`` selection.
    """
    text = text.strip()
    quoted_thread, quoted_reply = extract_quoted_thread(text)
    if quoted_thread is not None:
        current_thread = quoted_thread
        text = quoted_reply
        if not text:
            return (
                "reply",
                quoted_thread,
                f"已识别引用的 Codex 对话（编号尾部 {_thread_label(quoted_thread)}）。可发送 /continue、/暂停 或普通文本。",
            )
    if text.startswith(OUTBOUND_MARKER):
        return "ignore", current_thread, text[len(OUTBOUND_MARKER) :].lstrip()
    if not text:
        return "reply", current_thread, "请输入 /help 查看用法。"
    if len(text) > MAX_MESSAGE_CHARS:
        return "reply", current_thread, f"消息过长，最多允许 {MAX_MESSAGE_CHARS} 个字符。"

    command, separator, argument = text.partition(" ")
    command = command.lower()
    argument = argument.strip() if separator else ""

    if command in {"/help", "/帮助"}:
        return (
            "reply",
            current_thread,
            "QQ 控制用法：引用带对话编号的提醒或 /use <线程编号> 选择对话；/continue 继续；/暂停（或单独发送“暂停”）停止本轮；/send <内容> 发送正文；/forget 取消选择。",
        )
    if command in {"/use", "/选择"}:
        if not valid_thread_id(argument):
            return "reply", current_thread, "线程编号格式不正确，请使用完整的 Codex Desktop 线程编号。"
        return "reply", argument, f"已选择对话（编号尾部 {_thread_label(argument)}）。可发送 /continue、/暂停 或普通文本。"
    if command in {"/forget", "/取消"}:
        return "reply", None, "已取消当前 QQ 对话绑定。"
    if command in {"/current", "/当前"}:
        if current_thread:
            return "reply", current_thread, f"当前对话编号尾部：{_thread_label(current_thread)}。"
        return "reply", None, "当前还没有选择 Codex 对话。"
    if command in {"/continue", "/继续"}:
        if not current_thread:
            return "reply", None, "请先用 /use <线程编号> 选择 Codex 对话。"
        return "queue", current_thread, "继续"
    if command in {"/pause", "/暂停"} or text in {"暂停", "停止"}:
        if not current_thread:
            return "reply", None, "请先引用提醒或用 /use <线程编号> 选择要暂停的 Codex 对话。"
        if argument:
            return "reply", current_thread, "请单独发送 /暂停；需要发送普通正文可用 /send <内容>。"
        return "pause", current_thread, ""
    if command in {"/send", "/发送"}:
        if not current_thread:
            return "reply", None, "请先用 /use <线程编号> 选择 Codex 对话。"
        if not argument:
            return "reply", current_thread, "/send 后面需要跟要发给 Codex 的内容。"
        return "queue", current_thread, argument

    if not current_thread:
        return "reply", None, "请先用 /use <线程编号> 选择 Codex 对话；输入 /help 查看用法。"
    return "queue", current_thread, text


def _codex_command() -> list[str] | None:
    configured = os.environ.get("CODEX_QUEUE_BIN", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(["codex.exe", "codex.cmd", "codex"])
    for candidate in candidates:
        if not candidate:
            continue
        resolved = shutil.which(candidate) or (candidate if Path(candidate).exists() else "")
        if resolved:
            return [resolved]
    return None


def queue_to_desktop(thread_id: str, message: str, *, timeout_seconds: float = 20.0) -> tuple[bool, str]:
    if not valid_thread_id(thread_id):
        return False, "线程编号格式不正确。"
    if not message.strip():
        return False, "消息不能为空。"
    try:
        message.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False, "消息包含损坏的字符，未发送；请重新输入原文。"
    command = _codex_command()
    if command is None:
        return False, "找不到 codex 可执行文件。"
    command.extend(["queue", "--thread", thread_id, "--message", message])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=flags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "codex queue 超时。"
    except OSError as error:
        return False, f"启动 codex queue 失败：{type(error).__name__}。"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().replace("\r", " ").replace("\n", " ")
        if len(detail) > 240:
            detail = detail[:240] + "…"
        return False, f"codex queue 返回错误{(': ' + detail) if detail else '。'}"
    # Keep the exact native acknowledgement. A zero exit code alone says
    # nothing about whether the queued message has begun model execution.
    return True, (result.stdout or '').strip()[:4096]


class BridgeServer:
    def __init__(
        self,
        *,
        route_file: Path | None = None,
        queue_func: Callable[[str, str], tuple[bool, str]] = queue_to_desktop,
        pause_func: Callable[[str], tuple[bool, str]] = pause_desktop,
        controller: Any = None,
    ) -> None:
        self.route_file = route_file or routes_path()
        self.queue_func = queue_func
        self.pause_func = pause_func
        self.controller = controller
        self.routes = load_routes(self.route_file)
        self.routes_lock = threading.Lock()
        self.session_id = f"qq-desktop-{uuid.uuid4().hex}"

    def _route_for_user(self) -> str:
        return os.environ.get("CC_SESSION_KEY", "").strip()

    def _current_thread(self, user_key: str) -> str | None:
        with self.routes_lock:
            return self.routes.get(user_key)

    def _set_thread(self, user_key: str, thread_id: str | None) -> None:
        with self.routes_lock:
            if thread_id is None:
                self.routes.pop(user_key, None)
            else:
                self.routes[user_key] = thread_id
            save_routes(self.routes, self.route_file)

    def handle_prompt(self, text: str) -> str:
        user_key = self._route_for_user()
        if not user_key:
            return "当前 QQ 会话缺少身份标识，未执行任何操作。"
        current = self._current_thread(user_key)
        action, selected, payload = parse_control_message(text, current_thread=current)
        if selected != current or action == "reply" and text.strip().lower() in {"/forget", "/取消"}:
            self._set_thread(user_key, selected)
        if action == "ignore":
            return payload
        if action == "reply":
            return payload
        if action == "pause":
            ok, detail = self.pause_func(str(selected))
            prefix = f"对话（编号尾部 {_thread_label(str(selected))}）：" if ok else "未能确认暂停："
            return prefix + detail
        ok, detail = self.queue_func(str(selected), payload)
        if ok:
            return f"已排入 Codex Desktop 对话（编号尾部 {_thread_label(str(selected))}）。完成或出错后会继续推送提醒。"
        return f"未能排入 Codex Desktop：{detail}"

    @staticmethod
    def _response(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle_rpc(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params")
        if not isinstance(method, str):
            return []
        if method == "initialize":
            return [
                self._response(
                    request_id,
                    {
                        "protocolVersion": 1,
                        "agentCapabilities": {"loadSession": False},
                        "agentInfo": {
                            "name": "codex-desktop-qq-bridge",
                            "version": "0.2.0",
                        },
                    },
                )
            ]
        if method == "session/new":
            return [self._response(request_id, {"sessionId": self.session_id})]
        if method == "session/load":
            return [self._error(request_id, -32601, "session/load is not supported")]
        if method == "session/set_mode":
            return [self._response(request_id, {})]
        if method == "session/cancel":
            return [self._response(request_id, {})]
        if method == "session/prompt":
            text = extract_prompt_text(params)
            reply = self.controller.handle(params) if self.controller is not None else self.handle_prompt(text)
            return [
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": self.session_id,
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": reply},
                        },
                    },
                },
                self._response(request_id, {"stopReason": "end_turn"}),
            ]
        if request_id is None:
            return []
        return [self._error(request_id, -32601, f"method not implemented: {method}")]

    def run(self) -> int:
        for raw_line in sys.stdin:
            try:
                request = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(request, dict):
                continue
            for response in self.handle_rpc(request):
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        return 0


def _configure_protocol_stdio() -> None:
    # ACP 管道传递 UTF-8。Windows 的默认 GBK 不能用来读写该协议；
    # 这里直接设置流编码，避免只在某个启动终端设置环境变量后才工作。
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict", newline="\n", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> int:
    _configure_protocol_stdio()
    from control_router import ControlRouter
    return BridgeServer(controller=ControlRouter.runtime(queue_to_desktop)).run()


if __name__ == "__main__":
    raise SystemExit(main())
