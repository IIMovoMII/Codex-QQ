from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from qq_desktop_bridge import (
    BridgeServer,
    OUTBOUND_MARKER,
    extract_prompt_text,
    extract_quoted_thread,
    load_routes,
    parse_control_message,
    queue_to_desktop,
    save_routes,
    valid_thread_id,
)


THREAD = "00000000-0000-7000-8000-000000000002"


class QQDesktopBridgeTests(unittest.TestCase):
    def test_thread_id_validation_rejects_arguments(self) -> None:
        self.assertTrue(valid_thread_id(THREAD))
        self.assertFalse(valid_thread_id("../../secret"))
        self.assertFalse(valid_thread_id("short"))

    def test_parse_requires_explicit_selection(self) -> None:
        action, thread_id, payload = parse_control_message("继续", current_thread=None)
        self.assertEqual((action, thread_id), ("reply", None))
        self.assertIn("/use", payload)

    def test_parse_selection_and_commands(self) -> None:
        action, thread_id, payload = parse_control_message(f"/use {THREAD}", current_thread=None)
        self.assertEqual((action, thread_id), ("reply", THREAD))
        self.assertIn("已选择", payload)

        action, thread_id, payload = parse_control_message("/continue", current_thread=THREAD)
        self.assertEqual((action, thread_id, payload), ("queue", THREAD, "继续"))
        action, thread_id, payload = parse_control_message("/send 请继续", current_thread=THREAD)
        self.assertEqual((action, thread_id, payload), ("queue", THREAD, "请继续"))

    def test_pause_is_control_only_after_explicit_thread_selection(self) -> None:
        for command in ("/pause", "/暂停", "暂停", "停止"):
            self.assertEqual(parse_control_message(command, current_thread=THREAD), ("pause", THREAD, ""))
            action, selected, detail = parse_control_message(command, current_thread=None)
            self.assertEqual((action, selected), ("reply", None))
            self.assertIn("选择", detail)
        self.assertEqual(parse_control_message("/send 暂停", current_thread=THREAD), ("queue", THREAD, "暂停"))

    def test_pause_via_quote_overrides_previous_thread_without_queueing_text(self) -> None:
        quote = f"[引用消息]\n【任务提醒】\n对话编号：{THREAD}\n\n/暂停"
        self.assertEqual(parse_control_message(quote, current_thread="another-thread"), ("pause", THREAD, ""))

    def test_bridge_pause_uses_native_control_and_reports_failure(self) -> None:
        calls: list[str] = []

        def fake_pause(thread_id: str) -> tuple[bool, str]:
            calls.append(thread_id)
            return False, "该对话未载入。"

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"CC_SESSION_KEY": "qqbot:pause-test"}, clear=False
        ), patch("qq_desktop_bridge.queue_to_desktop") as queue:
            server = BridgeServer(route_file=Path(directory) / "routes.json", queue_func=queue,
                                  pause_func=fake_pause)
            server.handle_prompt(f"/use {THREAD}")
            self.assertIn("未能确认暂停", server.handle_prompt("/暂停"))
            self.assertEqual(calls, [THREAD])
            queue.assert_not_called()

    def test_extract_prompt_text_and_outbound_marker(self) -> None:
        self.assertEqual(
            extract_prompt_text({"prompt": [{"type": "text", "text": "你好"}, {"type": "image"}]}),
            "你好",
        )
        action, thread_id, payload = parse_control_message(
            OUTBOUND_MARKER + "【测试】提醒", current_thread=THREAD
        )
        self.assertEqual((action, thread_id, payload), ("ignore", THREAD, "【测试】提醒"))

    def test_quote_extracts_thread_and_only_queues_new_reply(self) -> None:
        quoted = (
            "[引用消息]\n"
            "【项目】Codex 任务已完成\n\n"
            "任务完成。\n\n"
            "模型：gpt-test\n\n"
            f"对话编号：{THREAD}\n\n"
            "继续"
        )
        self.assertEqual(extract_quoted_thread(quoted), (THREAD, "继续"))
        action, thread_id, payload = parse_control_message(quoted, current_thread=None)
        self.assertEqual((action, thread_id, payload), ("queue", THREAD, "继续"))

    def test_quote_without_reply_selects_thread_without_queueing(self) -> None:
        quoted = f"[引用消息]\n提醒\n对话编号：{THREAD}"
        action, thread_id, payload = parse_control_message(quoted, current_thread=None)
        self.assertEqual(action, "reply")
        self.assertEqual(thread_id, THREAD)
        self.assertIn("已识别引用", payload)

    def test_routes_are_atomic_json_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "routes.json"
            save_routes({"qqbot:user": THREAD}, path)
            self.assertEqual(load_routes(path), {"qqbot:user": THREAD})

    def test_bridge_queues_selected_thread_and_keeps_user_routes_separate(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_queue(thread_id: str, message: str) -> tuple[bool, str]:
            calls.append((thread_id, message))
            return True, ""

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"CC_SESSION_KEY": "qqbot:user-a"}, clear=False
        ):
            server = BridgeServer(route_file=Path(directory) / "routes.json", queue_func=fake_queue)
            self.assertIn("已选择", server.handle_prompt(f"/use {THREAD}"))
            self.assertIn("已排入", server.handle_prompt("/continue"))
            self.assertEqual(calls, [(THREAD, "继续")])
            self.assertEqual(load_routes(Path(directory) / "routes.json"), {"qqbot:user-a": THREAD})

    def test_acp_rpc_returns_update_then_prompt_result(self) -> None:
        server = BridgeServer(queue_func=lambda _thread, _message: (True, ""))
        with patch.dict(os.environ, {"CC_SESSION_KEY": "qqbot:user-a"}, clear=False):
            server.handle_prompt(f"/use {THREAD}")
            responses = server.handle_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "session/prompt",
                    "params": {"prompt": [{"type": "text", "text": "/continue"}]},
                }
            )
        self.assertEqual(responses[0]["method"], "session/update")
        self.assertEqual(responses[1]["result"]["stopReason"], "end_turn")

    def test_invalid_unicode_is_rejected_before_launching_codex(self) -> None:
        with patch("qq_desktop_bridge.subprocess.run") as launch:
            ok, detail = queue_to_desktop(THREAD, "继续\udced")
        self.assertFalse(ok)
        self.assertIn("损坏", detail)
        launch.assert_not_called()

    def test_queue_acknowledgement_is_preserved_without_claiming_started(self) -> None:
        queue_id = '00000000-0000-7000-8000-000000000004'
        ack = f'Queued message {queue_id} for thread {THREAD}.\n'
        with patch('qq_desktop_bridge._codex_command', return_value=['codex.exe']), \
             patch('qq_desktop_bridge.subprocess.run', return_value=subprocess.CompletedProcess(
                 ['codex.exe'], 0, ack, '')):
            ok, detail = queue_to_desktop(THREAD, '你好😀')
        self.assertTrue(ok)
        self.assertEqual(detail, ack.strip())


class BridgeProcessEncodingTests(unittest.TestCase):
    def run_bridge(self, prompts: list[str], *, expected_message: str | None = None) -> list[dict]:
        """用真实冷启动管道验收编码，不依赖测试进程的 UTF-8 设置。"""
        bridge = PACKAGE_DIR / "qq_desktop_bridge.py"
        bootstrap_setup = (
            "import runpy,sys,os; from pathlib import Path; sys.path.insert(0,str(Path(sys.argv[1]).parent)); "
            "sys.path.insert(0,str(Path(sys.argv[1]).parents[2]/'tests')); "
            "from test_control_router import FakeCatalog,FakeBackend; "
            "from control_router import ControlRouter; from control_store import ControlStore; "
            "ControlRouter.runtime=classmethod(lambda cls,queue:cls(ControlStore(Path(os.environ['CC_DATA_DIR'])/'control.sqlite'),FakeCatalog(),FakeBackend(),queue)); "
            "m=runpy.run_path(sys.argv[1],run_name='bridge_encoding_test'); "
        )
        command = [sys.executable, "-c", bootstrap_setup + "sys.exit(m['main']())", str(bridge)]
        if expected_message is not None:
            # 替身只检查子进程收到的参数，禁止向真实 Codex 对话写入。
            probe = (
                "import json,sys; "
                "expected=json.loads(sys.argv[1]); "
                "actual=sys.argv[sys.argv.index('--message')+1]; "
                "actual.encode('utf-8',errors='strict'); "
                "sys.exit(0 if actual==expected else 2)"
            )
            bootstrap = bootstrap_setup + (
                "m['queue_to_desktop'].__globals__['_codex_command']="
                "lambda:[sys.executable,'-X','utf8','-c',sys.argv[2],sys.argv[3]]; "
                "sys.exit(m['main']())"
            )
            command = [
                sys.executable, "-c", bootstrap, str(bridge), probe,
                json.dumps(expected_message, ensure_ascii=True),
            ]
        requests = [
            {"jsonrpc": "2.0", "id": index, "method": "session/prompt",
             "params": {"prompt": [{"type": "text", "text": text}]}}
            for index, text in enumerate(prompts, 1)
        ]
        import re
        import time
        for request, original in zip(requests, prompts):
            quote, text = "", original
            if original.startswith("[引用消息]"):
                match = re.search(r"对话编号：[A-Za-z0-9_-]{20,100}", original)
                quote, text = original[len("[引用消息]\n"):match.end()], original[match.end():].strip()
            request["params"]["_meta"] = {"cc_connect": {"version": 1, "platform": "qqbot", "session_key": "qqbot:encoding-test", "user_id": "encoding-test", "message_id": str(request["id"]), "sent_at_ms": time.time()*1000, "connection_ready_at_ms": 1000, "text": text, "quoted_text": quote}}
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.update({
                "PYTHONUTF8": "0", "PYTHONIOENCODING": "gbk",
                "CC_SESSION_KEY": "qqbot:encoding-test", "CC_DATA_DIR": directory,
            })
            # Start the ACP bridge before stamping/sending the test events.
            # This mirrors the real gateway: a prompt arriving after the
            # bridge generation is ready is valid, while a pre-start buffered
            # event must be discarded by the restart cutoff.
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env)
            try:
                process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 0,
                    "method": "session/new", "params": {}}) + "\n").encode("utf-8"))
                process.stdin.flush()
                handshake = process.stdout.readline()
                live_time = time.time() * 1000
                for request in requests:
                    request["params"]["_meta"]["cc_connect"]["sent_at_ms"] = live_time
                live_data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests).encode("utf-8")
                process.stdin.write(live_data)
                process.stdin.close()
                output = handshake + process.stdout.read()
                error = process.stderr.read()
                returncode = process.wait(timeout=15)
                process.stdout.close()
                process.stderr.close()
            except BaseException:
                process.kill()
                process.wait(timeout=5)
                raise
        self.assertEqual(returncode, 0, error.decode("utf-8", errors="replace"))
        messages = [json.loads(line) for line in output.decode("utf-8", errors="strict").splitlines()]
        return [message for message in messages if message.get("id") != 0]

    def test_entrypoint_reads_chinese_quote_and_replies_in_utf8(self) -> None:
        responses = self.run_bridge([f"[引用消息]\n【中文提醒😀】\n对话编号：{THREAD}"])
        reply = responses[0]["params"]["update"]["content"]["text"]
        self.assertIn("已识别引用的 Codex 对话", reply)
        self.assertIn(THREAD[-6:], reply)

    def test_chinese_multiline_and_emoji_reach_queue_unchanged_after_cold_start(self) -> None:
        message = "做得好\n请继续，不要停止 😀"
        responses = self.run_bridge([f"/use {THREAD}", f"[引用消息]\n【已选定】\n对话编号：{THREAD}\n\n{message}"], expected_message=message)
        reply = responses[2]["params"]["update"]["content"]["text"]
        self.assertIn("已提交给 Codex", reply)

    def test_quote_routes_only_new_chinese_reply_after_cold_start(self) -> None:
        message = "做得好，继续。"
        responses = self.run_bridge(
            [f"[引用消息]\n【完成提醒】\n对话编号：{THREAD}\n\n{message}"],
            expected_message=message,
        )
        self.assertIn("已提交", responses[0]["params"]["update"]["content"]["text"])


if __name__ == "__main__":
    unittest.main()
