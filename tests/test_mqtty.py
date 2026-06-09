from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

from mqtty.mqtty import (
    AvailableSerialPort,
    available_port_from_message,
    discovered_port_uri,
    main,
    resolve_mqtt_uri,
)


class MQTTYDiscoveryTests(unittest.TestCase):
    def test_available_port_from_message_reads_alias_payload(self) -> None:
        port = available_port_from_message(
            'testbench/serial/port-a/_mqtty/available',
            b'{"port":"port-a","alias":"/dev/ttyACM0"}',
            'testbench/serial',
        )

        self.assertIsNotNone(port)
        assert port is not None
        self.assertEqual(port.port, 'port-a')
        self.assertEqual(port.alias, '/dev/ttyACM0')

    def test_available_port_from_message_reads_nested_base_payload(self) -> None:
        port = available_port_from_message(
            'testbench/mark-desktop/port-a/_mqtty/available',
            b'{"port":"port-a","alias":"/dev/ttyACM0"}',
            'testbench',
        )

        self.assertIsNotNone(port)
        assert port is not None
        self.assertEqual(port.port, 'mark-desktop/port-a')
        self.assertEqual(port.alias, '/dev/ttyACM0')

    def test_available_port_from_message_uses_topic_port_as_alias_fallback(self) -> None:
        port = available_port_from_message(
            'testbench/serial/port-a/_mqtty/available',
            b'1',
            'testbench/serial',
        )

        self.assertIsNotNone(port)
        assert port is not None
        self.assertEqual(port.port, 'port-a')
        self.assertEqual(port.alias, 'port-a')

    def test_discovered_port_uri_appends_port_to_base_uri(self) -> None:
        self.assertEqual(
            discovered_port_uri('mqtt://broker.local/testbench/serial/', 'port-a'),
            'mqtt://broker.local/testbench/serial/port-a',
        )

    def test_discovered_port_uri_appends_nested_path_to_base_uri(self) -> None:
        self.assertEqual(
            discovered_port_uri('mqtt://broker.local/testbench/', 'mark-desktop/port-a'),
            'mqtt://broker.local/testbench/mark-desktop/port-a',
        )

    def test_resolve_mqtt_uri_keeps_explicit_full_uri(self) -> None:
        self.assertEqual(
            resolve_mqtt_uri('mqtt://other.local/testbench/port-a', 'mqtt://broker.local/testbench'),
            'mqtt://other.local/testbench/port-a',
        )

    def test_resolve_mqtt_uri_expands_relative_path_against_default(self) -> None:
        self.assertEqual(
            resolve_mqtt_uri('port-a', 'mqtt://broker.local/testbench/mark-desktop'),
            'mqtt://broker.local/testbench/mark-desktop/port-a',
        )

    def test_resolve_mqtt_uri_returns_none_without_uri_or_default(self) -> None:
        self.assertIsNone(resolve_mqtt_uri(None, None))

    def test_main_errors_without_uri_or_default(self) -> None:
        stderr = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('mqtty.mqtty.sys.stderr', stderr):
            with self.assertRaises(SystemExit) as ctx:
                main(['-l'])

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('mqtt_uri is required unless MQTTY_URI is set', stderr.getvalue())

    def test_list_uses_mqtty_uri_default(self) -> None:
        stdout = io.StringIO()
        ports = [AvailableSerialPort('port-a', '/dev/ttyACM0')]
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench/mark-desktop'}, clear=True),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports) as list_ports,
            patch('mqtty.mqtty.sys.stdout', stdout),
        ):
            main(['-l'])

        list_ports.assert_called_once_with('mqtt://broker.local/testbench/mark-desktop', 1.0)
        self.assertEqual(stdout.getvalue(), 'mqtt://broker.local/testbench/mark-desktop/port-a (/dev/ttyACM0)\n')

    def test_list_explicit_uri_overrides_mqtty_uri_default(self) -> None:
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench/mark-desktop'}, clear=True),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=[]) as list_ports,
        ):
            main(['-l', 'mqtt://other.local/testbench/other-host'])

        list_ports.assert_called_once_with('mqtt://other.local/testbench/other-host', 1.0)


if __name__ == '__main__':
    unittest.main()
