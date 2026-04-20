from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from mqtty import log_replay
from mqtty.log_replay import Replayer


class ReplayerOutputTests(unittest.TestCase):
    def test_write_stdout_bytes_retries_on_blocking_and_partial_writes(self) -> None:
        replayer = Replayer(Path("capture.jsonl.zst"))
        stdout = MagicMock()
        stdout.fileno.return_value = 9

        with (
            patch("mqtty.log_replay.sys.stdout", stdout),
            patch(
                "mqtty.log_replay.os.write",
                side_effect=[BlockingIOError, 2, 3],
            ) as write_mock,
            patch("mqtty.log_replay.time.sleep") as sleep_mock,
        ):
            replayer._write_stdout_bytes(b"hello")

        sleep_mock.assert_called_once_with(0.01)
        self.assertEqual(write_mock.call_count, 3)
        self.assertEqual(write_mock.call_args_list[0].args[0], 9)
        self.assertEqual(bytes(write_mock.call_args_list[0].args[1]), b"hello")
        self.assertEqual(write_mock.call_args_list[1].args[0], 9)
        self.assertEqual(bytes(write_mock.call_args_list[1].args[1]), b"hello")
        self.assertEqual(write_mock.call_args_list[2].args[0], 9)
        self.assertEqual(bytes(write_mock.call_args_list[2].args[1]), b"llo")

    def test_write_stdout_text_encodes_utf8(self) -> None:
        replayer = Replayer(Path("capture.jsonl.zst"))
        with patch.object(replayer, "_write_stdout_bytes") as write_bytes_mock:
            replayer._write_stdout_text("Speed: 1.00x\r\n")

        write_bytes_mock.assert_called_once_with(b"Speed: 1.00x\r\n")


class ReplayerFollowModeTests(unittest.TestCase):
    def test_enter_live_follow_mode_resets_speed_and_instant(self) -> None:
        replayer = Replayer(Path("capture.jsonl.zst"), follow=True)
        replayer.speed_factor = 8.0
        replayer.instant = True

        with patch.object(replayer, "_announce_speed") as announce_speed_mock:
            replayer._enter_live_follow_mode()
            replayer._enter_live_follow_mode()

        self.assertTrue(replayer.live_following)
        self.assertEqual(replayer.speed_factor, 1.0)
        self.assertFalse(replayer.instant)
        announce_speed_mock.assert_called_once()

    def test_enter_live_follow_mode_keeps_default_speed_silent(self) -> None:
        replayer = Replayer(Path("capture.jsonl.zst"), follow=True)
        with patch.object(replayer, "_announce_speed") as announce_speed_mock:
            replayer._enter_live_follow_mode()

        self.assertTrue(replayer.live_following)
        self.assertEqual(replayer.speed_factor, 1.0)
        self.assertFalse(replayer.instant)
        announce_speed_mock.assert_not_called()


class LogReplayMainTests(unittest.TestCase):
    def test_main_returns_130_on_keyboard_interrupt(self) -> None:
        with (
            patch("mqtty.log_replay.sys.argv", ["mqtty-log-replay", "capture.jsonl.zst"]),
            patch("mqtty.log_replay.Replayer") as replayer_cls,
        ):
            replayer_cls.return_value.run.side_effect = KeyboardInterrupt()
            rc = log_replay.main()

        self.assertEqual(rc, 130)

    def test_main_returns_zero_on_success(self) -> None:
        with (
            patch("mqtty.log_replay.sys.argv", ["mqtty-log-replay", "capture.jsonl.zst"]),
            patch("mqtty.log_replay.Replayer") as replayer_cls,
        ):
            rc = log_replay.main()

        self.assertEqual(rc, 0)
        replayer_cls.return_value.run.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
