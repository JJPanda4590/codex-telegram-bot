import unittest

from tgboter.telegram_bot import TelegramCodexBot


class ToolCommandClassificationTests(unittest.TestCase):
    def test_rg_with_line_numbers_is_search(self) -> None:
        self.assertEqual(TelegramCodexBot._classify_tool_command("rg -n TODO tgboter/telegram_bot.py"), "search")

    def test_shell_wrapped_rg_with_line_numbers_is_search(self) -> None:
        command = '/bin/zsh -lc \'rg -n "TODO|FIXME" tgboter/telegram_bot.py\''
        self.assertEqual(TelegramCodexBot._classify_tool_command(command), "search")

    def test_sed_print_mode_is_read(self) -> None:
        self.assertEqual(TelegramCodexBot._classify_tool_command("sed -n '1,40p' tgboter/telegram_bot.py"), "read")

    def test_shell_wrapped_sed_print_mode_is_read(self) -> None:
        command = "bash -lc \"sed -n '1,40p' tgboter/telegram_bot.py\""
        self.assertEqual(TelegramCodexBot._classify_tool_command(command), "read")

    def test_json_array_command_is_unwrapped(self) -> None:
        command = '["sed", "-n", "1,40p", "tgboter/telegram_bot.py"]'
        self.assertEqual(TelegramCodexBot._classify_tool_command(command), "read")

    def test_venv_python_heredoc_is_run(self) -> None:
        command = "./.venv/bin/python - <<'PY'\nimport telegram\nprint(telegram.__version__)\nPY"
        self.assertEqual(TelegramCodexBot._classify_tool_command(command), "run")


if __name__ == "__main__":
    unittest.main()
