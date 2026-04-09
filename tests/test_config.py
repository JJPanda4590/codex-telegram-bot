from __future__ import annotations

import unittest

from tgboter.config import Config


class ConfigTests(unittest.TestCase):
    def test_validate_uses_codex_only(self) -> None:
        config = Config(
            telegram_bot_token="token",
            whitelist=[1],
            codex_cli_path="/bin/echo",
        )

        config.validate()

        self.assertEqual(config.cli_path(), "/bin/echo")


if __name__ == "__main__":
    unittest.main()
