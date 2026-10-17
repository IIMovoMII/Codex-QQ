from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from models import ErrorCategory, ErrorSummary


_LABELS = {
    ErrorCategory.AUTHENTICATION: "认证失败",
    ErrorCategory.PERMISSION: "权限或访问被拒绝",
    ErrorCategory.RATE_LIMIT: "请求过于频繁",
    ErrorCategory.QUOTA: "额度或用量受限",
    ErrorCategory.NETWORK: "网络连接失败",
    ErrorCategory.TIMEOUT: "连接或任务超时",
    ErrorCategory.WEBSOCKET: "实时连接失败",
    ErrorCategory.ENDPOINT: "接口地址或路由错误",
    ErrorCategory.MODEL: "模型不可用",
    ErrorCategory.CONTEXT_LIMIT: "上下文超过限制",
    ErrorCategory.INVALID_REQUEST: "请求内容不被接口接受",
    ErrorCategory.RESPONSE_FORMAT: "响应格式不兼容",
    ErrorCategory.SERVER: "服务端异常",
    ErrorCategory.TOOL: "工具执行失败",
    ErrorCategory.CANCELLED: "任务已取消",
    ErrorCategory.UNKNOWN: "未识别的 Codex 错误",
}


_SECRET_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"), "<密钥>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"), "Bearer <令牌>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "<令牌>"),
    (re.compile(r"(?i)(OPENAI_API_KEY|API_KEY|ACCESS_TOKEN|REFRESH_TOKEN)\s*[:=]\s*[^\s,;]+"), r"\1=<已隐藏>"),
)
_URL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?)://[^\s\]\[(){}<>\"']+")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\"'<>|]+\\)*[^\s\"'<>|]*")
_UNIX_PATH_PATTERN = re.compile(r"(?<!\w)/(?:Users|home|var|tmp)/[^\s\"']+")
_HTTP_STATUS_PATTERN = re.compile(r"(?<!\d)([1-5]\d{2})(?!\d)")


def _flatten(value: Any, *, prefix: str = "", depth: int = 0) -> list[tuple[str, Any]]:
    if depth > 6:
        return []
    if isinstance(value, Mapping):
        flattened: list[tuple[str, Any]] = []
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten(child, prefix=child_prefix, depth=depth + 1))
        return flattened
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        flattened = []
        for index, child in enumerate(value[:20]):
            flattened.extend(_flatten(child, prefix=f"{prefix}[{index}]", depth=depth + 1))
        return flattened
    return [(prefix, value)]


def _first_text(flattened: list[tuple[str, Any]], preferred_names: tuple[str, ...]) -> str:
    for preferred in preferred_names:
        for path, value in flattened:
            if path.lower().endswith(preferred) and isinstance(value, str) and value.strip():
                return value.strip()
    for _, value in flattened:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_scalar(flattened: list[tuple[str, Any]], names: tuple[str, ...]) -> str | None:
    for name in names:
        for path, value in flattened:
            if path.lower().endswith(name) and value is not None and not isinstance(value, (Mapping, list, tuple)):
                text = str(value).strip()
                if text:
                    return text
    return None


def redact_error_text(text: str, *, limit: int = 500) -> str:
    cleaned = " ".join(text.replace("\x00", " ").split())
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = _URL_PATTERN.sub("<接口地址>", cleaned)
    cleaned = _WINDOWS_PATH_PATTERN.sub("<本地路径>", cleaned)
    cleaned = _UNIX_PATH_PATTERN.sub("<本地路径>", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned or "Codex 没有提供可显示的错误说明。"


def _normalized_fingerprint_text(text: str) -> str:
    value = text.lower()
    value = _URL_PATTERN.sub("<url>", value)
    value = _WINDOWS_PATH_PATTERN.sub("<path>", value)
    value = _UNIX_PATH_PATTERN.sub("<path>", value)
    value = re.sub(r"\b(?:item|rs|msg|call|fc|thread|turn)_[a-z0-9_-]+\b", "<id>", value)
    value = re.sub(r"\b[0-9a-f]{16,}\b", "<hex>", value)
    value = re.sub(r"\d+", "<n>", value)
    return " ".join(value.split())[:1000]


def _contains(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _classify(text: str, structured: str, code: str | None, status: int | None) -> tuple[ErrorCategory, bool]:
    combined = " ".join(part for part in (structured, code or "", text) if part).lower()

    if status in (401, 403) or _contains(
        combined,
        r"unauthori[sz]ed",
        r"invalid[_ -]?api[_ -]?key",
        r"authentication",
        r"oauth.*(?:expired|invalid)",
        r"missing environment variable.*(?:openai_api_key|access_token)",
    ):
        return ErrorCategory.AUTHENTICATION, False
    if _contains(combined, r"permission denied", r"access denied", r"approval.*denied", r"sandbox.*denied"):
        return ErrorCategory.PERMISSION, False
    if status == 429 and _contains(combined, r"quota", r"usage[_ -]?limit", r"insufficient[_ -]?quota", r"credit"):
        return ErrorCategory.QUOTA, False
    if _contains(combined, r"usage[_ -]?limit[_ -]?exceeded"):
        return ErrorCategory.QUOTA, False
    if status == 429 or _contains(combined, r"rate[_ -]?limit", r"too many requests"):
        return ErrorCategory.RATE_LIMIT, True
    if _contains(combined, r"context window", r"context_length", r"maximum context", r"too many tokens", r"token limit"):
        return ErrorCategory.CONTEXT_LIMIT, False
    if _contains(combined, r"websocket", r"wss://", r"response stream disconnected", r"stream.*disconnect"):
        return ErrorCategory.WEBSOCKET, True
    if _contains(combined, r"timed? out", r"timeout", r"deadline exceeded"):
        return ErrorCategory.TIMEOUT, True
    if _contains(combined, r"dns", r"connection (?:failed|refused|reset|closed)", r"network", r"tls", r"certificate", r"proxy"):
        return ErrorCategory.NETWORK, True
    if status == 404 and _contains(combined, r"model"):
        return ErrorCategory.MODEL, False
    if _contains(
        combined,
        r"model.*(?:not found|unavailable|unsupported|does not exist)",
        r"unknown model",
        r"no available channel for model",
    ):
        return ErrorCategory.MODEL, False
    if status is not None and 500 <= status <= 599:
        return ErrorCategory.SERVER, True
    if _contains(
        combined,
        r"server[_ -]?overloaded",
        r"service unavailable",
        r"internal server error",
        r"bad gateway",
        r"upstream[_ -]?(?:request[_ -]?)?(?:failed|error)",
        r"bad_response_status_code",
        r"openai_error",
    ):
        return ErrorCategory.SERVER, True
    if status == 404 or _contains(combined, r"invalid url", r"not found", r"endpoint", r"route", r"/v1/responses"):
        return ErrorCategory.ENDPOINT, False
    if _contains(combined, r"expected an id that begins with", r"invalid ['\"]?input\[", r"invalid[_ -]?request", r"bad request", r"malformed request"):
        return ErrorCategory.INVALID_REQUEST, False
    if _contains(combined, r"response.*(?:format|schema|decode|parse)", r"invalid json", r"unexpected response", r"protocol"):
        return ErrorCategory.RESPONSE_FORMAT, False
    if _contains(combined, r"cancelled", r"canceled", r"interrupted", r"aborted"):
        return ErrorCategory.CANCELLED, False
    if _contains(combined, r"tool.*(?:failed|error)", r"mcp.*(?:failed|error)"):
        return ErrorCategory.TOOL, False
    return ErrorCategory.UNKNOWN, False


def normalize_error(value: Any) -> ErrorSummary:
    if value is None:
        value = {"message": "Codex 没有提供错误对象。"}
    if isinstance(value, str):
        value = {"message": value}
    elif not isinstance(value, Mapping):
        value = {"message": repr(value)}

    flattened = _flatten(value)
    message = _first_text(flattened, (".message", "message", ".detail", "detail", ".error"))
    code = _first_scalar(flattened, (".code", "code", ".error_code", "error_code", ".type", "type"))
    structured_kind = _first_scalar(
        flattened,
        (".codex_error_info", "codex_error_info", ".kind", "kind", ".category", "category"),
    )

    status: int | None = None
    raw_status = _first_scalar(flattened, (".status", "status", ".status_code", "status_code", ".http_status", "http_status"))
    if raw_status and raw_status.isdigit():
        candidate = int(raw_status)
        if 100 <= candidate <= 599:
            status = candidate
    if status is None:
        match = _HTTP_STATUS_PATTERN.search(message)
        if match:
            candidate = int(match.group(1))
            if 400 <= candidate <= 599:
                status = candidate

    structured_text = " ".join(
        str(value)
        for path, value in flattened
        if "codex_error_info" in path.lower() or path.lower().endswith((".kind", ".category"))
    )
    category, transient = _classify(message, structured_text, code, status)
    public_message = redact_error_text(message)
    fingerprint_input = json.dumps(
        {
            "category": category.value,
            "status": status,
            "code": code,
            "structured": structured_kind,
            "message": _normalized_fingerprint_text(message),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:20]

    return ErrorSummary(
        category=category,
        label=_LABELS[category],
        public_message=public_message,
        fingerprint=fingerprint,
        http_status=status,
        code=code,
        structured_kind=structured_kind,
        transient=transient,
    )
