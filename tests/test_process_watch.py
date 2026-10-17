from __future__ import annotations

import sys
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "codex_desktop_weixin"
sys.path.insert(0, str(PACKAGE_DIR))

from process_watch import _is_codex_desktop_image


class ProcessRecognitionTests(unittest.TestCase):
    def test_accepts_versioned_store_install_path(self) -> None:
        self.assertTrue(
            _is_codex_desktop_image(
                r"c:\program files\windowsapps\openai.codex_26.928.3736.0_x64__2p2nqsd0c76g0\app\chatgpt.exe"
            )
        )

    def test_accepts_unversioned_or_dash_package_path(self) -> None:
        self.assertTrue(_is_codex_desktop_image(r"c:\apps\openai.codex\app\chatgpt.exe"))
        self.assertTrue(_is_codex_desktop_image(r"c:\apps\openai.codex-preview\app\chatgpt.exe"))

    def test_rejects_unrelated_chatgpt_path(self) -> None:
        self.assertFalse(_is_codex_desktop_image(r"c:\apps\openai.chatgpt\app\chatgpt.exe"))
        self.assertFalse(_is_codex_desktop_image(r"c:\apps\openai.codex_1\chatgpt.exe"))


if __name__ == "__main__":
    unittest.main()
