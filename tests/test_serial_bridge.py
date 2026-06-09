from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from mqtty.serial_bridge import (
    DEVICE_SERIAL_INPUT_TOPIC,
    MQTTBridgeConfig,
    SerialBridge,
    SerialBridgeConfig,
    SerialPortState,
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

    def test_load_config_reads_serial_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / 'bridge.toml'
            path.write_text(
                '[mqtt]\n'
                'topic_base = "testbench/mark-pantry"\n'
                '\n'
                '[serial_aliases]\n'
                '"platform-ci_hdrc.1-usb-0:1.4.3.2:1.0" = "/dev/ttyACM0"\n',
                encoding='utf-8',
            )
            cfg = load_config(path)

        self.assertEqual(
            cfg.serial_aliases,
            {'platform-ci_hdrc.1-usb-0:1.4.3.2:1.0': '/dev/ttyACM0'},
        )

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


class _EqZeroNoInt:
    def __eq__(self, other: object) -> bool:
        return other == 0

    def __str__(self) -> str:
        return 'Success'


class SerialBridgeMQTTTests(unittest.TestCase):
    def test_mqtt_connect_accepts_non_int_reason_code(self) -> None:
        cfg = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/mark-desktop'),
            usb_match=None,
        )
        bridge = SerialBridge(cfg)
        mqtt_client = MagicMock()
        bridge.mqtt_client = mqtt_client

        bridge.mqtt_connect()

        callback = bridge.mqtt_client.on_connect
        callback_client = MagicMock()
        callback(callback_client, None, {}, _EqZeroNoInt(), None)

        callback_client.subscribe.assert_called_once_with('testbench/mark-desktop/+/device_serial_input')

    def test_port_alias_prefers_config_then_alias_file_then_real_path(self) -> None:
        port = 'platform-ci_hdrc.1-usb-0:1.4.3.2:1.0'

        with tempfile.TemporaryDirectory() as temp_dir:
            serial_base = Path(temp_dir)
            alias_file = serial_base / f'{port}.alias'
            alias_file.write_text('/dev/ttyACM0\n', encoding='utf-8')

            cfg = SerialBridgeConfig(
                mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/mark-desktop'),
                usb_match=None,
            )
            bridge = SerialBridge(cfg, serial_base_path=serial_base)
            self.assertEqual(bridge.port_alias(port, '/dev/ttyUSB9'), '/dev/ttyACM0')

            override_cfg = SerialBridgeConfig(
                mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/mark-desktop'),
                usb_match=None,
                serial_aliases={port: 'console'},
            )
            override_bridge = SerialBridge(override_cfg, serial_base_path=serial_base)
            self.assertEqual(override_bridge.port_alias(port, '/dev/ttyUSB9'), 'console')

            alias_file.unlink()
            self.assertEqual(bridge.port_alias(port, '/dev/ttyUSB9'), '/dev/ttyUSB9')

    def test_list_candidate_ports_ignores_alias_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            serial_base = Path(temp_dir)
            (serial_base / 'platform-ci_hdrc.1-usb-0:1.4.3.2:1.0').touch()
            (serial_base / 'platform-ci_hdrc.1-usb-0:1.4.3.2:1.0.alias').touch()

            cfg = SerialBridgeConfig(
                mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/mark-desktop'),
                usb_match=None,
            )
            bridge = SerialBridge(cfg, serial_base_path=serial_base)

            self.assertEqual(bridge._list_candidate_ports(), {'platform-ci_hdrc.1-usb-0:1.4.3.2:1.0'})

    def test_publish_port_availability_is_retained_json(self) -> None:
        cfg = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/mark-desktop'),
            usb_match=None,
        )
        bridge = SerialBridge(cfg)
        bridge.mqtt_client = MagicMock()

        bridge.publish_port_availability('port-a', '/dev/ttyACM0')

        bridge.mqtt_client.publish.assert_called_once()
        topic, payload = bridge.mqtt_client.publish.call_args.args
        self.assertEqual(topic, 'testbench/mark-desktop/port-a/_mqtty/available')
        self.assertEqual(json.loads(payload), {'port': 'port-a', 'alias': '/dev/ttyACM0'})
        self.assertTrue(bridge.mqtt_client.publish.call_args.kwargs['retain'])

    def test_clear_port_availability_clears_retained_topic(self) -> None:
        cfg = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/mark-desktop'),
            usb_match=None,
        )
        bridge = SerialBridge(cfg)
        bridge.mqtt_client = MagicMock()

        bridge.clear_port_availability('port-a')

        bridge.mqtt_client.publish.assert_called_once_with(
            'testbench/mark-desktop/port-a/_mqtty/available',
            b'',
            retain=True,
        )

    def test_stop_serial_thread_clears_retained_availability(self) -> None:
        cfg = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/mark-desktop'),
            usb_match=None,
        )
        bridge = SerialBridge(cfg)
        bridge.mqtt_client = MagicMock()
        connection = MagicMock()
        thread = MagicMock()
        state = SerialPortState(
            connection=connection,
            thread=thread,
            serial_output_topic='testbench/mark-desktop/port-a/device_serial_output',
            real_device_path='/dev/ttyACM0',
            alias='/dev/ttyACM0',
        )
        bridge.serial_ports['port-a'] = state
        bridge.opened_real_devices.add('/dev/ttyACM0')

        bridge.stop_serial_thread('port-a')

        thread.join.assert_called_once_with(timeout=2.0)
        connection.close.assert_called_once_with()
        self.assertNotIn('port-a', bridge.serial_ports)
        self.assertNotIn('/dev/ttyACM0', bridge.opened_real_devices)
        bridge.mqtt_client.publish.assert_called_once_with(
            'testbench/mark-desktop/port-a/_mqtty/available',
            b'',
            retain=True,
        )


if __name__ == '__main__':
    unittest.main()
