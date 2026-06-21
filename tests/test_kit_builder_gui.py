from __future__ import annotations

import unittest

from gui.kit_builder_gui import _normalize_provider


class KitBuilderGUITests(unittest.TestCase):
    def test_normalize_provider_keeps_supported_value(self) -> None:
        self.assertEqual(_normalize_provider("ollama"), "ollama")

    def test_normalize_provider_resets_invalid_cached_value(self) -> None:
        self.assertEqual(_normalize_provider("qwen3.6:27b"), "ollama")
        self.assertEqual(_normalize_provider(""), "ollama")


if __name__ == "__main__":
    unittest.main()
