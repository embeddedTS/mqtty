from __future__ import annotations

import unittest
from pathlib import Path

from mqtty.serial_log import build_parser, prefix_log_key, prefix_log_path


class SerialLogParserTests(unittest.TestCase):
    def test_device_subcommand_parses_full_device_url(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "device",
                "mqtt://broker.local/testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0",
                "capture.jsonl.zst",
            ]
        )

        self.assertEqual(args.command, "device")
        self.assertEqual(
            args.mqtt_uri,
            "mqtt://broker.local/testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0",
        )
        self.assertEqual(args.logfile, Path("capture.jsonl.zst"))

    def test_prefix_subcommand_parses_prefix_url(self) -> None:
        parser = build_parser()

        args = parser.parse_args(
            [
                "prefix",
                "mqtt://broker.local/testbench/mark-pantry",
                "logs",
            ]
        )

        self.assertEqual(args.command, "prefix")
        self.assertEqual(args.mqtt_uri, "mqtt://broker.local/testbench/mark-pantry")
        self.assertEqual(args.outdir, Path("logs"))


class PrefixLogRoutingTests(unittest.TestCase):
    def test_prefix_log_key_for_server_prefix(self) -> None:
        key = prefix_log_key(
            "testbench/mark-pantry",
            "testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0/device_serial_output",
        )

        self.assertEqual(key, ("platform-ci_hdrc.1-usb-0:1.4.3.2:1.0",))

    def test_prefix_log_key_for_base_prefix(self) -> None:
        key = prefix_log_key(
            "testbench",
            "testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0/device_serial_output",
        )

        self.assertEqual(key, ("mark-pantry", "platform-ci_hdrc.1-usb-0:1.4.3.2:1.0"))

    def test_prefix_log_key_ignores_non_serial_output_topics(self) -> None:
        key = prefix_log_key(
            "testbench/mark-pantry",
            "testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0/device_serial_input",
        )

        self.assertIsNone(key)

    def test_prefix_log_key_ignores_exact_prefix_topic(self) -> None:
        key = prefix_log_key(
            "testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0",
            "testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0/device_serial_output",
        )

        self.assertIsNone(key)

    def test_prefix_log_path_keeps_relative_topic_structure(self) -> None:
        path = prefix_log_path(
            Path("logs"),
            ("mark-pantry", "platform-ci_hdrc.1-usb-0:1.4.3.2:1.0"),
            "2026-04-19",
        )

        self.assertEqual(
            path,
            Path("logs/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0/2026-04-19_replay.jsonl.zst"),
        )


if __name__ == "__main__":
    unittest.main()
