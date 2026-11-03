"""Small CI smoke suite for the user-facing QQ control path.

The repository keeps the larger regression modules for release audits and
local debugging.  CI runs this curated set so a routine push checks the
actual install/help/quote/send flows without turning every historical fault
injection into a required gate.
"""

from __future__ import annotations

import importlib
import unittest


_CASES = (
    # Installation and first-run guidance.
    ('test_install_app', 'InstallWizardTests', 'test_first_run_prints_qq_bot_setup_guide'),
    # Help and the core quote -> select -> send path.
    ('test_mobile_copy', 'MobileCopyTests', 'test_pages_fit_a_mobile_message_without_markdown_or_ids'),
    ('test_mobile_copy', 'HelpRoutingTests', 'test_help_topics_and_quoted_help_never_submit_a_task'),
    ('test_control_router', 'ControlRouterTests', 'test_title_selection_uses_project_and_fixed_snapshot'),
    ('test_control_router', 'ControlRouterTests', 'test_busy_message_returns_queue_number_and_status_card'),
    ('test_control_router', 'ControlRouterTests', 'test_old_split_rollout_alias_is_canonicalized_before_native_dispatch'),
    ('test_control_router', 'ControlRouterTests', 'test_missing_catalog_row_loads_silently_before_routing_quoted_message'),
    ('test_control_router', 'ControlRouterTests', 'test_duplicate_event_does_not_repeat_but_same_text_new_event_does'),
    ('test_control_router', 'ControlRouterTests', 'test_offline_reject_and_reconnect_cutoff_never_replays'),
    # User-facing action cards and creation flow.
    ('test_control_actions', 'ActionRouterTests', 'test_question_answer_stop_is_not_pause_or_new_model_message'),
    ('test_control_actions', 'ActionRouterTests', 'test_queue_selection_converts_or_deletes_exact_id_without_confirmation'),
    ('test_control_actions', 'ActionRouterTests', 'test_permission_choice_requires_complete_explicit_confirmation'),
    ('test_control_actions', 'ActionRouterTests', 'test_compression_acceptance_is_not_completion_and_cannot_be_repeated_on_card'),
    ('test_creation_commands', 'CreationTests', 'test_wizard_is_readonly_until_full_confirmation_then_new_card_targets_child'),
    ('test_creation_commands', 'CreationTests', 'test_unknown_creation_cannot_be_repeated_on_the_same_wizard'),
    # Native submission safety and notification routing.
    ('test_message_submission', 'MessageSubmissionTests', 'test_paused_then_new_text_starts_next_turn_and_never_uses_queue_or_steer'),
    ('test_message_submission', 'MessageSubmissionTests', 'test_transient_resume_state_is_reread_before_rejecting'),
    ('test_message_submission', 'MessageSubmissionTests', 'test_pending_submission_stops_resume_retry'),
    ('test_notifier', 'NotificationDispatcherTests', 'test_notification_contains_full_thread_reference_for_qq_quote'),
    ('test_notifier', 'NotificationDispatcherTests', 'test_dead_letter_is_terminal_for_repeated_emit_and_restart'),
    ('test_tailer', 'TailerTests', 'test_split_rollout_path_uses_stable_session_uuid'),
    ('test_monitor', 'ControlStatusIndexTests', 'test_old_split_rollout_checkpoint_is_rebound_without_duplicate_active_turn'),
)


def load_tests(loader: unittest.TestLoader, _tests: unittest.TestSuite, _pattern: str | None) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    for module_name, class_name, method_name in _CASES:
        module = importlib.import_module(module_name)
        suite.addTest(getattr(module, class_name)(method_name))
    return suite


if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(load_tests(unittest.defaultTestLoader, unittest.TestSuite(), None))
