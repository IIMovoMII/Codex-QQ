from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from models import Signal, SignalKind, TurnState
from state_machine import StateMachine


BASE = datetime(2026, 8, 23, tzinfo=timezone.utc)


def signal(kind: SignalKind, seconds: int = 0, **data):
    return Signal(kind, observed_at=BASE + timedelta(seconds=seconds), session_id="s", turn_id="t", source="test", data=data)


class StateMachineTests(unittest.TestCase):
    def new_machine(self) -> StateMachine:
        machine = StateMachine(
            stop_completion_grace=timedelta(seconds=5),
        )
        machine.apply(signal(SignalKind.TURN_STARTED))
        return machine

    def test_success(self) -> None:
        machine = self.new_machine()
        notes = machine.apply(signal(SignalKind.TURN_COMPLETED, 2, last_assistant_message="完成", error=None))
        self.assertEqual([note.state for note in notes], [TurnState.COMPLETED])

    def test_embedded_error_wins_over_task_complete_name(self) -> None:
        machine = self.new_machine()
        notes = machine.apply(signal(SignalKind.TURN_COMPLETED, 2, last_assistant_message="", error={"message": "401 Unauthorized"}))
        self.assertEqual(notes[0].state, TurnState.FAILED)

    def test_old_error_without_recovery_is_failure(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.ERROR_OBSERVED, 1, error={"message": "connection failed"}))
        notes = machine.apply(signal(SignalKind.TURN_COMPLETED, 2, last_assistant_message="", error=None))
        self.assertEqual(notes[0].state, TurnState.FAILED)

    def test_error_followed_by_real_answer_is_recovered(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.ERROR_OBSERVED, 1, error={"message": "temporary network error"}))
        machine.apply(signal(SignalKind.ASSISTANT_MESSAGE, 2, message="已经恢复并完成"))
        notes = machine.apply(signal(SignalKind.TURN_COMPLETED, 3, last_assistant_message="已经恢复并完成", error=None))
        self.assertEqual(notes[0].state, TurnState.COMPLETED)

    def test_terminal_turn_releases_large_evidence_after_rendering(self) -> None:
        machine = self.new_machine()
        message = "完成：" + ("A" * 200_000)
        notes = machine.apply(signal(SignalKind.TURN_COMPLETED, 2, last_assistant_message=message, error=None))

        self.assertEqual(notes[0].state, TurnState.COMPLETED)
        runtime = machine.turns[("s", "t")]
        self.assertEqual(runtime.last_assistant_message, "")
        self.assertEqual(runtime.errors, [])
        self.assertEqual(runtime.project_name, "")
        self.assertEqual(runtime.model, "")

    def test_active_evidence_is_bounded(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.ASSISTANT_MESSAGE, 1, message="A" * 200_000))
        for index in range(20):
            machine.apply(signal(SignalKind.ERROR_OBSERVED, 2 + index, error={"message": f"temporary-{index}"}))

        runtime = machine.turns[("s", "t")]
        self.assertLessEqual(len(runtime.last_assistant_message), 16 * 1024)
        self.assertLessEqual(len(runtime.errors), 8)

    def test_waiting_states(self) -> None:
        machine = self.new_machine()
        self.assertEqual(machine.apply(signal(SignalKind.PERMISSION_REQUESTED, 1))[0].state, TurnState.WAITING_APPROVAL)
        machine = self.new_machine()
        self.assertEqual(machine.apply(signal(SignalKind.USER_INPUT_REQUESTED, 1))[0].state, TurnState.WAITING_INPUT)
        self.assertEqual(machine.apply(signal(SignalKind.USER_INPUT_RESOLVED, 2)), [])

    def test_long_silence_never_creates_notification(self) -> None:
        machine = self.new_machine()
        self.assertEqual(machine.tick(BASE + timedelta(hours=6)), [])

    def test_process_exit_during_maintenance_is_not_crash(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.MAINTENANCE_STARTED, 1))
        self.assertEqual(machine.apply(signal(SignalKind.PROCESS_STOPPED, 2)), [])
        self.assertEqual(machine.active_runtimes(), [])

    def test_process_exit_is_silent_and_releases_turn_state_without_claiming_failure(self):
        machine = self.new_machine()
        machine.apply(signal(SignalKind.ASSISTANT_MESSAGE, 1, message='部分回复'))
        machine.apply(signal(SignalKind.STOP_OBSERVED, 2))
        self.assertEqual(machine.apply(signal(SignalKind.PROCESS_STOPPED, 3)), [])
        self.assertEqual(machine.apply(signal(SignalKind.PROCESS_STOPPED, 4)), [])
        runtime = machine.turns[('s', 't')]
        self.assertTrue(runtime.terminal)
        self.assertEqual(runtime.state, TurnState.DISCONNECTED)
        self.assertNotIn(TurnState.CRASHED, runtime.notified_states)
        self.assertEqual(runtime.last_assistant_message, '')
        self.assertEqual(machine.active_runtimes(), [])
        self.assertEqual(machine.pending_stop_keys, set())
        self.assertEqual(machine.tick(BASE + timedelta(hours=1)), [])

    def test_stop_never_declares_success_without_terminal_record(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.ASSISTANT_MESSAGE, 1, message="完成"))
        self.assertEqual(machine.apply(signal(SignalKind.STOP_OBSERVED, 2)), [])
        self.assertEqual(machine.tick(BASE + timedelta(seconds=6)), [])
        self.assertEqual(machine.tick(BASE + timedelta(seconds=20)), [])
        self.assertFalse(machine.turns[("s", "t")].terminal)

    def test_activity_after_stop_cancels_stop_candidate(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.STOP_OBSERVED, 1, stop_hook_active=False))
        machine.apply(signal(SignalKind.ACTIVITY, 2))
        self.assertNotIn(("s", "t"), machine.pending_stop_keys)
        self.assertIsNone(machine.turns[("s", "t")].stop_observed_at)

    def test_concurrent_stop_continuation_never_emits_a_premature_result(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.STOP_OBSERVED, 1, stop_hook_active=False))
        machine.apply(signal(SignalKind.STOP_OBSERVED, 2, stop_hook_active=True))
        self.assertEqual(machine.tick(BASE + timedelta(seconds=20)), [])

        machine.apply(signal(SignalKind.ACTIVITY, 21))
        self.assertEqual(machine.tick(BASE + timedelta(minutes=5)), [])
        self.assertFalse(machine.turns[("s", "t")].terminal)

    def test_new_turn_retires_all_older_unfinished_turns(self) -> None:
        machine = StateMachine()
        machine.apply(Signal(SignalKind.TURN_STARTED, observed_at=BASE, session_id="s", turn_id="old-1", source="test"))
        machine.active_turn_by_session["s"] = "old-2"
        machine.apply(Signal(SignalKind.ACTIVITY, observed_at=BASE, session_id="s", turn_id="old-2", source="test"))
        machine.apply(Signal(SignalKind.TURN_STARTED, observed_at=BASE, session_id="s", turn_id="new", source="test"))

        self.assertTrue(machine.turns[("s", "old-1")].terminal)
        self.assertTrue(machine.turns[("s", "old-2")].terminal)
        self.assertEqual([runtime.turn_id for runtime in machine.active_runtimes()], ["new"])

    def test_late_old_terminal_does_not_clear_new_active_turn(self) -> None:
        machine = StateMachine()
        machine.apply(Signal(SignalKind.TURN_STARTED, observed_at=BASE, session_id="s", turn_id="old", source="test"))
        machine.apply(Signal(SignalKind.TURN_STARTED, observed_at=BASE, session_id="s", turn_id="new", source="test"))
        machine.apply(Signal(SignalKind.TURN_COMPLETED, observed_at=BASE, session_id="s", turn_id="old", source="test"))
        self.assertEqual(machine.active_turn_by_session["s"], "new")

    def test_terminal_rollout_wins_during_stop_grace(self) -> None:
        machine = self.new_machine()
        machine.apply(signal(SignalKind.STOP_OBSERVED, 1))
        notes = machine.apply(signal(SignalKind.TURN_COMPLETED, 2, error={"message": "503 upstream"}))
        self.assertEqual(notes[0].state, TurnState.FAILED)
        self.assertEqual(machine.tick(BASE + timedelta(seconds=20)), [])


if __name__ == "__main__":
    unittest.main()
