from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from models import Signal, SignalKind


_MAX_SESSION_META_BYTES = 1024 * 1024


def is_desktop_rollout(path: str | Path) -> bool | None:
    """Classify a rollout without scanning its conversation history.

    Codex Desktop main tasks currently identify themselves with
    ``session_meta.payload.source == \"vscode\"``.  CLI-created sessions use
    ``exec`` and subagents use an object.  ``None`` means the first record is not
    complete yet, so the directory watcher should retry on the next write.
    """
    transcript = Path(path)
    with transcript.open("rb") as stream:
        raw = stream.readline(_MAX_SESSION_META_BYTES + 1)
    if not raw.endswith(b"\n"):
        return None
    if len(raw) > _MAX_SESSION_META_BYTES:
        return False
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return False
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return False
    return payload.get("source") == "vscode"


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


class RolloutSchemaError(ValueError):
    pass


def _assistant_text_from_response_item(payload: dict[str, Any]) -> str:
    if payload.get("role") != "assistant":
        return ""
    chunks: list[str] = []
    content_rows = payload.get('content')
    if not isinstance(content_rows, list):
        raise RolloutSchemaError('invalid-message-content')
    for content in content_rows or []:
        if not isinstance(content, dict):
            raise RolloutSchemaError('invalid-content-part')
        if any(name in content and not isinstance(content[name], str) for name in ('text', 'output_text')):
            raise RolloutSchemaError('invalid-content-text')
        text = content.get("text") or content.get("output_text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return "\n".join(chunks)


def parse_rollout_line(
    line: str,
    *,
    session_id: str | None = None,
    transcript_path: str | None = None,
) -> list[Signal]:
    record = json.loads(line)
    if not isinstance(record, dict):
        raise RolloutSchemaError('invalid-record')
    observed_at = _parse_time(record.get("timestamp"))
    if observed_at is None:
        # Never turn an unknown event time into "now": that can make an old
        # record pass the monitor's not_before boundary and emit a duplicate.
        raise RolloutSchemaError('invalid-timestamp')
    outer_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise RolloutSchemaError('invalid-payload')
    for identity in ('turn_id', 'turnId'):
        if payload.get(identity) is not None and not isinstance(payload[identity], str):
            raise RolloutSchemaError('invalid-turn-identity')

    common = {
        "observed_at": observed_at,
        "session_id": session_id,
        "transcript_path": transcript_path,
        "source": "rollout",
    }
    signals: list[Signal] = []

    if outer_type == "session_meta":
        meta_id = payload.get("id")
        if meta_id is not None and not isinstance(meta_id, str):
            raise RolloutSchemaError('invalid-session-identity')
        signals.append(
            Signal(
                SignalKind.SESSION_STARTED,
                session_id=str(meta_id or session_id or ""),
                data={"cwd": payload.get("cwd"), "model_provider": payload.get("model_provider")},
                **{key: value for key, value in common.items() if key != "session_id"},
            )
        )
        return signals

    inner_type = payload.get("type")
    turn_id = payload.get("turn_id")
    if turn_id is not None:
        turn_id = str(turn_id)

    if outer_type == "event_msg":
        if inner_type == "task_started":
            signals.append(Signal(SignalKind.TURN_STARTED, turn_id=turn_id, data={"started_at": payload.get("started_at")}, **common))
        elif inner_type == "task_complete":
            signals.append(
                Signal(
                    SignalKind.TURN_COMPLETED,
                    turn_id=turn_id,
                    data={
                        "last_assistant_message": payload.get("last_agent_message") or "",
                        "error": payload.get("error"),
                        "duration_ms": payload.get("duration_ms"),
                    },
                    **common,
                )
            )
        elif inner_type == "turn_aborted":
            signals.append(
                Signal(
                    SignalKind.TURN_ABORTED,
                    turn_id=turn_id,
                    data={"reason": payload.get("reason") or "interrupted"},
                    **common,
                )
            )
        elif inner_type == "error":
            signals.append(Signal(SignalKind.ERROR_OBSERVED, turn_id=turn_id, data={"error": payload}, **common))
        elif inner_type == "agent_message":
            message = payload.get("message")
            if isinstance(message, str) and message:
                signals.append(Signal(SignalKind.ASSISTANT_MESSAGE, turn_id=turn_id, data={"message": message}, **common))
        elif inner_type == 'user_message':
            from async_questions import parse_answers
            answers = parse_answers(payload.get('message'))
            signals.append(Signal(SignalKind.ASYNC_QUESTION_ANSWERED if answers else SignalKind.ACTIVITY,
                                  turn_id=turn_id, data={'answers': answers} if answers else {}, **common))
        else:
            signals.append(Signal(SignalKind.ACTIVITY, turn_id=turn_id, data={"event_type": str(inner_type or "event_msg")}, **common))
        return signals

    if outer_type == "response_item":
        if inner_type == "message":
            message = _assistant_text_from_response_item(payload)
            if message:
                signals.append(Signal(SignalKind.ASSISTANT_MESSAGE, turn_id=turn_id, data={"message": message}, **common))
            else:
                signals.append(Signal(SignalKind.ACTIVITY, turn_id=turn_id, data={"event_type": "message"}, **common))
            if payload.get('role') == 'user':
                from async_questions import input_answers
                answers = input_answers(payload.get('content'))
                if answers:
                    signals.append(Signal(SignalKind.ASYNC_QUESTION_ANSWERED,
                                          turn_id=turn_id, data={'answers': answers}, **common))
        elif inner_type in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or "")
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            kind = SignalKind.USER_INPUT_REQUESTED if name == "request_user_input" else SignalKind.TOOL_STARTED
            signals.append(Signal(kind, turn_id=turn_id, data={"tool_name": name, "call_id": call_id, "historical": True}, **common))
            from async_questions import TOOL_NAMES, parse_call
            if name in TOOL_NAMES:
                question = parse_call(payload)
                if question:
                    signals.append(Signal(SignalKind.ASYNC_QUESTION_CREATED, turn_id=turn_id,
                                          data=question, **common))
        elif inner_type in ("function_call_output", "custom_tool_call_output"):
            signals.append(
                Signal(
                    SignalKind.TOOL_COMPLETED,
                    turn_id=turn_id,
                    data={"call_id": str(payload.get("call_id") or payload.get("id") or ""), "historical": True},
                    **common,
                )
            )
        else:
            signals.append(Signal(SignalKind.ACTIVITY, turn_id=turn_id, data={"event_type": str(inner_type or outer_type)}, **common))
        return signals

    return [Signal(SignalKind.ACTIVITY, turn_id=turn_id, data={"event_type": str(outer_type or "unknown")}, **common)]


def iter_rollout_signals(path: str | Path, *, start_offset: int = 0) -> Iterable[tuple[int, Signal]]:
    transcript = Path(path)
    session_id: str | None = None
    with transcript.open("rb") as stream:
        stream.seek(start_offset)
        while True:
            line_offset = stream.tell()
            raw = stream.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                break
            try:
                text = raw.decode("utf-8")
                signals = parse_rollout_line(text, session_id=session_id, transcript_path=str(transcript))
            except (UnicodeDecodeError, json.JSONDecodeError, RolloutSchemaError):
                continue
            for signal in signals:
                if signal.kind == SignalKind.SESSION_STARTED and signal.session_id:
                    session_id = signal.session_id
                elif session_id and not signal.session_id:
                    signal = Signal(
                        kind=signal.kind,
                        observed_at=signal.observed_at,
                        session_id=session_id,
                        turn_id=signal.turn_id,
                        transcript_path=signal.transcript_path,
                        source=signal.source,
                        data=signal.data,
                    )
                yield stream.tell(), signal
