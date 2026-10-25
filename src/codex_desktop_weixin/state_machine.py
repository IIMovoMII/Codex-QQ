from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from errors import normalize_error, redact_error_text
from models import Confidence, ErrorSummary, Notification, Signal, SignalKind, TurnState


MAX_RETAINED_ASSISTANT_CHARS = 16 * 1024
MAX_RETAINED_ERRORS = 8
MAX_RETAINED_TERMINAL_TURNS = 512


@dataclass
class TurnRuntime:
    session_id: str
    turn_id: str
    state: TurnState = TurnState.RUNNING
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_assistant_message: str = ""
    last_assistant_at: datetime | None = None
    errors: list[tuple[datetime, ErrorSummary]] = field(default_factory=list)
    terminal: bool = False
    notified_states: set[TurnState] = field(default_factory=set)
    notification_epochs: dict[TurnState, int] = field(default_factory=dict)
    stop_observed_at: datetime | None = None
    project_name: str = ""
    model: str = ""


class StateMachine:
    def __init__(
        self,
        *,
        stop_completion_grace: timedelta = timedelta(seconds=12),
    ) -> None:
        self.stop_completion_grace = stop_completion_grace
        self.turns: dict[tuple[str, str], TurnRuntime] = {}
        self.active_turn_by_session: dict[str, str] = {}
        # Track only unfinished turns. This preserves the old behavior of
        # retiring every stale turn for a session without scanning all history
        # on each new turn.
        self.active_turns_by_session: dict[str, set[str]] = {}
        self.pending_stop_keys: set[tuple[str, str]] = set()
        self.session_context: dict[str, dict[str, str]] = {}
        self._retired_keys: set[tuple[str, str]] = set()
        self._retired_order: deque[tuple[str, str]] = deque()
        self.maintenance = False

    def _clear_active_if_matches(self, runtime: TurnRuntime) -> None:
        if self.active_turn_by_session.get(runtime.session_id) == runtime.turn_id:
            self.active_turn_by_session.pop(runtime.session_id, None)
        active = self.active_turns_by_session.get(runtime.session_id)
        if active is not None:
            active.discard(runtime.turn_id)
            if not active:
                self.active_turns_by_session.pop(runtime.session_id, None)

    def _mark_active(self, runtime: TurnRuntime) -> None:
        self.active_turns_by_session.setdefault(runtime.session_id, set()).add(runtime.turn_id)

    def _retire_without_notification(self, runtime: TurnRuntime) -> None:
        """Retire a superseded/ambiguous turn without guessing its outcome."""
        runtime.terminal = True
        self.pending_stop_keys.discard((runtime.session_id, runtime.turn_id))
        self._clear_active_if_matches(runtime)
        self._compact_runtime(runtime)
        self._remember_retired((runtime.session_id, runtime.turn_id))

    def _remember_retired(self, key: tuple[str, str]) -> None:
        if key in self._retired_keys:
            return
        self._retired_keys.add(key)
        self._retired_order.append(key)
        while len(self._retired_order) > MAX_RETAINED_TERMINAL_TURNS:
            old = self._retired_order.popleft()
            self._retired_keys.discard(old)
            runtime = self.turns.get(old)
            if runtime is not None and runtime.terminal and old[1] not in self.active_turns_by_session.get(old[0], set()):
                self.turns.pop(old, None)

    @staticmethod
    def _compact_runtime(runtime: TurnRuntime) -> None:
        """Keep a small tombstone while releasing bulky per-turn evidence."""
        runtime.last_assistant_message = ""
        runtime.last_assistant_at = None
        runtime.errors.clear()
        runtime.project_name = ""
        runtime.model = ""
        runtime.stop_observed_at = None

    @staticmethod
    def _remember_assistant_message(runtime: TurnRuntime, message: str) -> None:
        # The notification only needs a short preview. Keep a bounded prefix
        # while active, then release it when the turn becomes terminal.
        if len(message) <= MAX_RETAINED_ASSISTANT_CHARS:
            runtime.last_assistant_message = message
            return
        marker = "\n…[内容已截断，仅用于本地状态判断]…\n"
        budget = MAX_RETAINED_ASSISTANT_CHARS - len(marker)
        head = max(1, budget // 2)
        runtime.last_assistant_message = message[:head] + marker + message[-(budget - head):]

    def active_runtimes(self) -> list[TurnRuntime]:
        """Return at most the one current, non-terminal turn for each session."""
        result: list[TurnRuntime] = []
        for session_id, turn_ids in self.active_turns_by_session.items():
            for turn_id in turn_ids:
                runtime = self.turns.get((session_id, turn_id))
                if runtime is not None and not runtime.terminal:
                    result.append(runtime)
        return result

    def _context_from_signal(self, signal: Signal) -> dict[str, str]:
        project_name = signal.data.get("project_name")
        if not project_name:
            cwd = signal.data.get("cwd")
            if isinstance(cwd, str) and cwd:
                project_name = Path(cwd).name
        model = signal.data.get("model")
        return {
            "project_name": str(project_name or "")[:200],
            "model": str(model or "")[:200],
        }

    def _update_context(self, runtime: TurnRuntime, signal: Signal) -> None:
        context = self._context_from_signal(signal)
        if context["project_name"]:
            runtime.project_name = context["project_name"]
        if context["model"]:
            runtime.model = context["model"]

    def _key(self, signal: Signal) -> tuple[str, str] | None:
        session = signal.session_id or "unknown-session"
        turn = signal.turn_id or self.active_turn_by_session.get(session)
        if not turn:
            return None
        return session, turn

    def _runtime(self, signal: Signal, *, create: bool = True) -> TurnRuntime | None:
        key = self._key(signal)
        if key is None:
            return None
        runtime = self.turns.get(key)
        if runtime is None and key in self._retired_keys:
            return None
        if runtime is None and create:
            # Records can arrive without TURN_STARTED. Keep the one-active-turn
            # invariant here too, and never let an older signal resurrect a
            # turn after a newer one has become active.
            for prior_turn in tuple(self.active_turns_by_session.get(key[0], set())):
                prior_runtime = self.turns.get((key[0], prior_turn))
                if prior_runtime is None or prior_runtime.terminal:
                    continue
                # A late terminal record may complete an unknown old turn,
                # but must not manufacture a second active turn. Non-terminal
                # records with the same timestamp can still be the first
                # observable record for a new turn (the native stream often
                # batches timestamps at millisecond resolution), so retire
                # the old runtime in that case.
                if signal.kind not in (SignalKind.TURN_COMPLETED, SignalKind.TURN_ABORTED) and signal.observed_at <= prior_runtime.started_at:
                    return None
                if signal.kind not in (SignalKind.TURN_COMPLETED, SignalKind.TURN_ABORTED):
                    self._retire_without_notification(prior_runtime)
            context = self.session_context.get(key[0], {})
            runtime = TurnRuntime(
                session_id=key[0],
                turn_id=key[1],
                started_at=signal.observed_at,
                project_name=context.get("project_name", ""),
                model=context.get("model", ""),
            )
            self.turns[key] = runtime
            if signal.kind not in (SignalKind.TURN_COMPLETED, SignalKind.TURN_ABORTED):
                self.active_turn_by_session[key[0]] = key[1]
                self._mark_active(runtime)
        if runtime is not None:
            self._update_context(runtime, signal)
        return runtime

    def _notification(
        self,
        runtime: TurnRuntime,
        *,
        state: TurnState,
        confidence: Confidence,
        title: str,
        message: str,
        error: ErrorSummary | None = None,
        event_at: datetime | None = None,
        allow_repeat: bool = False,
    ) -> Notification | None:
        if not allow_repeat and state in runtime.notified_states:
            return None
        runtime.notified_states.add(state)
        runtime.state = state
        epoch = runtime.notification_epochs.get(state, 0)
        dedupe_key = f"{runtime.session_id}:{runtime.turn_id}:{state.value}"
        if state in (TurnState.WAITING_APPROVAL, TurnState.WAITING_INPUT):
            # Event time survives a monitor restart and keeps replayed hook
            # records idempotent. Fall back to an in-memory epoch only for
            # synthetic signals that have no event timestamp.
            marker = event_at.isoformat() if event_at is not None else str(epoch)
            dedupe_key += f":{marker}"
        return Notification(
            session_id=runtime.session_id,
            turn_id=runtime.turn_id,
            state=state,
            confidence=confidence,
            title=title,
            message=message,
            project_name=runtime.project_name,
            model=runtime.model,
            error=error,
            dedupe_key=dedupe_key,
        )

    def apply(self, signal: Signal) -> list[Notification]:
        if signal.kind == SignalKind.MAINTENANCE_STARTED:
            self.maintenance = True
            return []
        if signal.kind == SignalKind.MAINTENANCE_ENDED:
            self.maintenance = False
            return []

        session = signal.session_id or "unknown-session"
        if signal.kind == SignalKind.SESSION_STARTED:
            session = signal.session_id or "unknown-session"
            context = self._context_from_signal(signal)
            previous = self.session_context.get(session, {})
            self.session_context[session] = {
                "project_name": context["project_name"] or previous.get("project_name", ""),
                "model": context["model"] or previous.get("model", ""),
            }
            return []

        if signal.kind == SignalKind.SESSION_ENDED:
            # SessionEnd is authoritative lifecycle cleanup, not a task
            # result.  Without this branch a Desktop session that ends while
            # the process remains alive leaves a permanent active mapping.
            for (prior_session, _), runtime in tuple(self.turns.items()):
                if prior_session == session and not runtime.terminal:
                    self._retire_without_notification(runtime)
            self.active_turn_by_session.pop(session, None)
            self.active_turns_by_session.pop(session, None)
            self.session_context.pop(session, None)
            return []

        if signal.kind == SignalKind.TURN_STARTED:
            turn = signal.turn_id or f"unknown-{int(signal.observed_at.timestamp())}"
            # A Codex session has only one current turn. Old unfinished turns
            # are retired silently instead of retaining stale running state.
            if (session, turn) in self._retired_keys:
                return []
            for prior_turn in tuple(self.active_turns_by_session.get(session, set())):
                if prior_turn == turn:
                    continue
                prior_runtime = self.turns.get((session, prior_turn))
                if prior_runtime is not None and not prior_runtime.terminal:
                    self._retire_without_notification(prior_runtime)
            self.active_turn_by_session[session] = turn
            existing = self.turns.get((session, turn))
            if existing is not None and not existing.terminal:
                self._update_context(existing, signal)
                self._mark_active(existing)
                return []
            context = self.session_context.get(session, {})
            signal_context = self._context_from_signal(signal)
            runtime = TurnRuntime(
                session_id=session,
                turn_id=turn,
                started_at=signal.observed_at,
                project_name=signal_context["project_name"] or context.get("project_name", ""),
                model=signal_context["model"] or context.get("model", ""),
            )
            self.turns[(session, turn)] = runtime
            self._mark_active(runtime)
            return []

        runtime = self._runtime(signal)
        if runtime is None:
            return []
        if runtime.terminal and signal.kind not in (SignalKind.PROCESS_STARTED, SignalKind.MAINTENANCE_STARTED, SignalKind.MAINTENANCE_ENDED):
            return []

        notifications: list[Notification] = []
        if signal.kind in (
            SignalKind.ACTIVITY,
            SignalKind.ASSISTANT_MESSAGE,
            SignalKind.TOOL_STARTED,
            SignalKind.TOOL_COMPLETED,
            SignalKind.USER_INPUT_RESOLVED,
        ):
            # Activity after Stop means this turn continued (for example because
            # another concurrent Stop hook requested more work).
            for waiting_state in (TurnState.WAITING_INPUT, TurnState.WAITING_APPROVAL):
                if waiting_state in runtime.notified_states:
                    runtime.notification_epochs[waiting_state] = runtime.notification_epochs.get(waiting_state, 0) + 1
            runtime.stop_observed_at = None
            self.pending_stop_keys.discard((runtime.session_id, runtime.turn_id))
            runtime.notified_states.discard(TurnState.WAITING_INPUT)
            runtime.notified_states.discard(TurnState.WAITING_APPROVAL)
            if signal.kind == SignalKind.ASSISTANT_MESSAGE:
                message = signal.data.get("message")
                if isinstance(message, str) and message:
                    self._remember_assistant_message(runtime, message)
                    runtime.last_assistant_at = signal.observed_at
            runtime.state = TurnState.RUNNING
            return notifications

        if signal.kind == SignalKind.ERROR_OBSERVED:
            summary = normalize_error(signal.data.get("error"))
            runtime.errors.append((signal.observed_at, summary))
            if len(runtime.errors) > MAX_RETAINED_ERRORS:
                del runtime.errors[:-MAX_RETAINED_ERRORS]
            return []

        if signal.kind == SignalKind.STOP_OBSERVED:
            # Stop hooks for the same event run concurrently.  Another hook may
            # still ask Codex to continue, so Stop is only a short-lived marker;
            # it is never allowed to declare success or failure by itself.
            runtime.stop_observed_at = signal.observed_at
            self.pending_stop_keys.add((runtime.session_id, runtime.turn_id))
            return []

        if signal.kind == SignalKind.PERMISSION_REQUESTED:
            note = self._notification(
                runtime,
                state=TurnState.WAITING_APPROVAL,
                confidence=Confidence.CERTAIN,
                title="Codex 正在等待授权",
                message="任务需要你回到电脑确认权限后才能继续。",
                event_at=signal.observed_at,
            )
            return [note] if note else []

        if signal.kind == SignalKind.USER_INPUT_REQUESTED:
            if signal.data.get("historical"):
                return []
            note = self._notification(
                runtime,
                state=TurnState.WAITING_INPUT,
                confidence=Confidence.HIGH,
                title="Codex 正在等待输入",
                message="任务向你提出了问题，需要你回复后才能继续。",
                event_at=signal.observed_at,
            )
            return [note] if note else []

        if signal.kind == SignalKind.TURN_ABORTED:
            runtime.terminal = True
            self.pending_stop_keys.discard((runtime.session_id, runtime.turn_id))
            self._clear_active_if_matches(runtime)
            reason = redact_error_text(str(signal.data.get("reason") or "任务被中断。"), limit=240)
            note = self._notification(
                runtime,
                state=TurnState.INTERRUPTED,
                confidence=Confidence.CERTAIN,
                title="Codex 任务已中断",
                message=reason,
            )
            self._compact_runtime(runtime)
            self._remember_retired((runtime.session_id, runtime.turn_id))
            return [note] if note else []

        if signal.kind == SignalKind.TURN_COMPLETED:
            embedded_error = signal.data.get("error")
            final_message = signal.data.get("last_assistant_message")
            if isinstance(final_message, str) and final_message:
                self._remember_assistant_message(runtime, final_message)
                runtime.last_assistant_at = signal.observed_at

            failure: ErrorSummary | None = None
            if embedded_error is not None:
                failure = normalize_error(embedded_error)
            elif runtime.errors:
                last_error_at, last_error = runtime.errors[-1]
                recovered_after_error = bool(
                    runtime.last_assistant_at
                    and runtime.last_assistant_at > last_error_at
                    and runtime.last_assistant_message.strip()
                )
                if not recovered_after_error:
                    failure = last_error

            runtime.terminal = True
            self.pending_stop_keys.discard((runtime.session_id, runtime.turn_id))
            self._clear_active_if_matches(runtime)
            if failure is not None:
                note = self._notification(
                    runtime,
                    state=TurnState.FAILED,
                    confidence=Confidence.CERTAIN if embedded_error is not None else Confidence.HIGH,
                    title=f"Codex 任务失败：{failure.label}",
                    message=failure.public_message,
                    error=failure,
                )
            else:
                preview = redact_error_text(runtime.last_assistant_message or "任务已经完成，但没有可显示的最终回复。", limit=1200)
                note = self._notification(
                    runtime,
                    state=TurnState.COMPLETED,
                    confidence=Confidence.CERTAIN,
                    title="Codex 任务已完成",
                    message=preview,
                )
            self._compact_runtime(runtime)
            self._remember_retired((runtime.session_id, runtime.turn_id))
            return [note] if note else []

        if signal.kind == SignalKind.PROCESS_STOPPED:
            # Desktop may be closed or restarted normally. Its disappearance is
            # neither a task failure nor a result worth sending to QQ.
            runtime.state = TurnState.DISCONNECTED
            self._retire_without_notification(runtime)
            return []

        return []

    def tick(self, now: datetime | None = None) -> list[Notification]:
        now = now or datetime.now(timezone.utc)
        for key in tuple(self.pending_stop_keys):
            runtime = self.turns.get(key)
            if runtime is None or runtime.terminal:
                self.pending_stop_keys.discard(key)
                continue
            if runtime.state in (TurnState.WAITING_APPROVAL, TurnState.WAITING_INPUT):
                continue
            if runtime.stop_observed_at and now - runtime.stop_observed_at >= self.stop_completion_grace:
                # The marker expires, but the runtime remains active until an
                # authoritative terminal record, a new turn, or Desktop exit.
                # This avoids racing a slower concurrent Stop hook.
                self.pending_stop_keys.discard(key)
        return []
