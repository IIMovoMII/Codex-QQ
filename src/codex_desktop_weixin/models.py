from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SignalKind(StrEnum):
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    TURN_STARTED = "turn_started"
    ACTIVITY = "activity"
    ASSISTANT_MESSAGE = "assistant_message"
    ERROR_OBSERVED = "error_observed"
    TURN_COMPLETED = "turn_completed"
    TURN_ABORTED = "turn_aborted"
    STOP_OBSERVED = "stop_observed"
    PERMISSION_REQUESTED = "permission_requested"
    USER_INPUT_REQUESTED = "user_input_requested"
    USER_INPUT_RESOLVED = "user_input_resolved"
    ASYNC_QUESTION_CREATED = 'async_question_created'
    ASYNC_QUESTION_ANSWERED = 'async_question_answered'
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    PROCESS_STARTED = "process_started"
    PROCESS_STOPPED = "process_stopped"
    MAINTENANCE_STARTED = "maintenance_started"
    MAINTENANCE_ENDED = "maintenance_ended"


class TurnState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    QUESTION_AVAILABLE = 'question_available'
    POSSIBLE_STALL = "possible_stall"
    SUSPECTED_STALL = "suspected_stall"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    DISCONNECTED = 'disconnected'
    CRASHED = "crashed"


class Confidence(StrEnum):
    CERTAIN = "certain"
    HIGH = "high"
    INFERRED = "inferred"


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    NETWORK = "network"
    TIMEOUT = "timeout"
    WEBSOCKET = "websocket"
    ENDPOINT = "endpoint"
    MODEL = "model"
    CONTEXT_LIMIT = "context_limit"
    INVALID_REQUEST = "invalid_request"
    RESPONSE_FORMAT = "response_format"
    SERVER = "server"
    TOOL = "tool"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorSummary:
    category: ErrorCategory
    label: str
    public_message: str
    fingerprint: str
    http_status: int | None = None
    code: str | None = None
    structured_kind: str | None = None
    transient: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["category"] = self.category.value
        return result


@dataclass(frozen=True)
class Signal:
    kind: SignalKind
    observed_at: datetime = field(default_factory=utc_now)
    session_id: str | None = None
    turn_id: str | None = None
    transcript_path: str | None = None
    source: str = "unknown"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Notification:
    session_id: str
    turn_id: str
    state: TurnState
    confidence: Confidence
    title: str
    message: str
    project_name: str = ""
    model: str = ""
    created_at: datetime = field(default_factory=utc_now)
    error: ErrorSummary | None = None
    dedupe_key: str = ""
    question_call_id: str = ''
    question_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["state"] = self.state.value
        result["confidence"] = self.confidence.value
        result["created_at"] = self.created_at.isoformat()
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result
