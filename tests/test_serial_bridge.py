from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mqtty.serial_bridge import (
    DEVICE_SERIAL_INPUT_TOPIC,
    extract_port_name,
    join_topic_path,
    load_config,
    load_config_with_fallback,
    split_topic_path,
    usb_allowed,
)


class SerialBridgeHelpersTests(unittest.TestCase):
    def test_split_topic_path(self) -> None:
        self.assertEqual(split_topic_path('/a//b/c/'), ('a', 'b', 'c'))

    def test_join_topic_path(self) -> None:
        self.assertEqual(join_topic_path('/testbench/', '/mark-pantry/', 'port0', '/device_serial_output'), 'testbench/mark-pantry/port0/device_serial_output')

    def test_extract_port_name_matches_topic_base(self) -> None:
        topic = 'testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0/device_serial_input'
        self.assertEqual(
            extract_port_name(topic, 'testbench/mark-pantry'),
            'platform-ci_hdrc.1-usb-0:1.4.3.2:1.0',
        )

    def test_extract_port_name_rejects_nonmatching_suffix(self) -> None:
        topic = 'testbench/mark-pantry/platform-ci_hdrc.1-usb-0:1.4.3.2:1.0/device_serial_output'
        self.assertIsNone(extract_port_name(topic, 'testbench/mark-pantry'))

    def test_usb_allowed_with_none_allow_list(self) -> None:
        self.assertTrue(usb_allowed('1a86', '7523', None))

    def test_usb_allowed_with_wildcard_and_exact_entries(self) -> None:
        allow = (('1a86', '7523'), ('0403', '*'))
        self.assertTrue(usb_allowed('1a86', '7523', allow))
        self.assertTrue(usb_allowed('0403', '6001', allow))
        self.assertFalse(usb_allowed('10c4', 'ea60', allow))


class SerialBridgeConfigTests(unittest.TestCase):
    def test_load_config_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'bridge.toml'
            path.write_text('[mqtt]\n', encoding='utf-8')
            cfg = load_config(path)

        self.assertEqual(cfg.mqtt.host, 'localhost')
        self.assertEqual(cfg.mqtt.port, 1883)
        self.assertEqual(cfg.mqtt.topic_base, 'testbench')
        self.assertIsNone(cfg.usb_match)

    def test_load_config_reads_usb_match_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'bridge.toml'
            path.write_text(
                '[mqtt]\n'
                'host = "mqtt.example.com"\n'
                'port = 1885\n'
                'topic_base = "testbench/mark-pantry"\n'
                '\n'
                '[[usb_match]]\n'
                'vid = "1A86"\n'
                'pid = "7523"\n'
                '\n'
                '[[usb_match]]\n'
                'vid = "0403"\n'
                'pid = "*"\n',
                encoding='utf-8',
            )
            cfg = load_config(path)

        self.assertEqual(cfg.mqtt.host, 'mqtt.example.com')
        self.assertEqual(cfg.mqtt.port, 1885)
        self.assertEqual(cfg.mqtt.topic_base, 'testbench/mark-pantry')
        self.assertEqual(cfg.usb_match, (('1a86', '7523'), ('0403', '*')))

    def test_load_config_with_fallback_prefers_first_existing_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            first = Path(temp_dir) / 'first.toml'
            second = Path(temp_dir) / 'second.toml'
            second.write_text('[mqtt]\nhost = "h2"\n', encoding='utf-8')
            first.write_text('[mqtt]\nhost = "h1"\n', encoding='utf-8')

            cfg, used_path = load_config_with_fallback(first)

        self.assertEqual(used_path, first)
        self.assertEqual(cfg.mqtt.host, 'h1')

    def test_load_config_with_fallback_raises_when_no_default_exists(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_config_with_fallback(Path('/definitely/not/here/mqtty-serial-bridge.toml'))

    def test_device_serial_input_topic_name_constant(self) -> None:
        self.assertEqual(DEVICE_SERIAL_INPUT_TOPIC, 'device_serial_input')


if __name__ == '__main__':
    unittest.main()
