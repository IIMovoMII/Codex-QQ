from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from errors import normalize_error, redact_error_text
from models import ErrorCategory


class ErrorNormalizationTests(unittest.TestCase):
    def test_structured_and_text_variants(self) -> None:
        cases = [
            ({"message": "unexpected status 401 Unauthorized", "codex_error_info": "Other"}, ErrorCategory.AUTHENTICATION),
            ({"message": "429 insufficient_quota", "status": 429}, ErrorCategory.QUOTA),
            ({"message": "429 rate limit exceeded", "status_code": 429}, ErrorCategory.RATE_LIMIT),
            ({"message": "Responses WebSocket failed: stream disconnected"}, ErrorCategory.WEBSOCKET),
            ({"message": "request timed out after 30 seconds"}, ErrorCategory.TIMEOUT),
            ({"message": "connection reset by peer"}, ErrorCategory.NETWORK),
            ({"message": "404 model gpt-x not found", "status": 404}, ErrorCategory.MODEL),
            ({"message": "404 Not Found: invalid URL GET /v1/responses"}, ErrorCategory.ENDPOINT),
            ({"message": "maximum context length exceeded"}, ErrorCategory.CONTEXT_LIMIT),
            ({"message": "Invalid 'input[165].id': item_x. Expected an ID that begins with 'rs'."}, ErrorCategory.INVALID_REQUEST),
            ({"message": "503 Service Unavailable", "status": 503}, ErrorCategory.SERVER),
            ({"message": "503 Service Unavailable, url: https://relay.example/v1/responses"}, ErrorCategory.SERVER),
            ({"message": "temporarily unavailable", "codex_error_info": "server_overloaded"}, ErrorCategory.SERVER),
            ({"message": "try again later", "codex_error_info": "usage_limit_exceeded"}, ErrorCategory.QUOTA),
            ({"message": "Upstream request failed", "code": "upstream_error"}, ErrorCategory.SERVER),
            ({"message": "Missing environment variable: OPENAI_API_KEY"}, ErrorCategory.AUTHENTICATION),
            ({"message": "a brand new failure shape", "codex_error_info": {"kind": "FutureVariant"}}, ErrorCategory.UNKNOWN),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(normalize_error(payload).category, expected)

    def test_secrets_and_paths_are_redacted(self) -> None:
        text = "OPENAI_API_KEY=sk-super-secret-value at C:\\Users\\Alice\\secret.txt https://relay.example/v1?token=abc"
        redacted = redact_error_text(text)
        self.assertNotIn("super-secret", redacted)
        self.assertNotIn("Alice", redacted)
        self.assertNotIn("relay.example", redacted)

    def test_unknown_error_still_has_stable_summary(self) -> None:
        first = normalize_error({"message": "Future failure id item_abc 991"})
        second = normalize_error({"message": "Future failure id item_def 123"})
        self.assertEqual(first.category, ErrorCategory.UNKNOWN)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertTrue(first.public_message)


if __name__ == "__main__":
    unittest.main()
