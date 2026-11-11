from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from mqtty.mqtty import (
    CONNECTION_NOTICE,
    MQTTY,
    AvailableSerialPort,
    available_port_from_message,
    discovered_port_uri,
    main,
    resolve_mqtt_uri,
)
from mqtty.serial_bridge import MQTTBridgeConfig, SerialBridgeConfig


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

    def test_resolve_mqtt_uri_expands_discovered_alias_against_default(self) -> None:
        ports = [AvailableSerialPort('pci-0000:12:00.3-usb-0:1.1.1.3:1.0', 'usb-c')]

        self.assertEqual(
            resolve_mqtt_uri('usb-c', 'mqtt://broker.local/testbench/mark-desktop', ports),
            'mqtt://broker.local/testbench/mark-desktop/pci-0000:12:00.3-usb-0:1.1.1.3:1.0',
        )

    def test_resolve_mqtt_uri_expands_dev_path_alias_against_default(self) -> None:
        ports = [AvailableSerialPort('port-a', '/dev/ttyUSB0')]

        self.assertEqual(
            resolve_mqtt_uri('/dev/ttyUSB0', 'mqtt://broker.local/testbench/mark-desktop', ports),
            'mqtt://broker.local/testbench/mark-desktop/port-a',
        )

    def test_resolve_mqtt_uri_expands_discovered_port_against_default(self) -> None:
        ports = [AvailableSerialPort('port-a', 'usb-a')]

        self.assertEqual(
            resolve_mqtt_uri('port-a', 'mqtt://broker.local/testbench/mark-desktop', ports),
            'mqtt://broker.local/testbench/mark-desktop/port-a',
        )

    def test_resolve_mqtt_uri_rejects_unknown_bare_name_when_ports_are_known(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown serial port or alias 'foobar'"):
            resolve_mqtt_uri(
                'foobar',
                'mqtt://broker.local/testbench/mark-desktop',
                [AvailableSerialPort('port-a', 'usb-a')],
            )

    def test_resolve_mqtt_uri_allows_path_like_relative_topic_without_discovery_match(self) -> None:
        self.assertEqual(
            resolve_mqtt_uri(
                'nested/port-a',
                'mqtt://broker.local/testbench',
                [AvailableSerialPort('other-port', 'usb-a')],
            ),
            'mqtt://broker.local/testbench/nested/port-a',
        )

    def test_resolve_mqtt_uri_rejects_ambiguous_alias(self) -> None:
        ports = [
            AvailableSerialPort('port-a', 'console'),
            AvailableSerialPort('port-b', 'console'),
        ]

        with self.assertRaisesRegex(ValueError, "Ambiguous serial port or alias 'console'"):
            resolve_mqtt_uri('console', 'mqtt://broker.local/testbench', ports)

    def test_resolve_mqtt_uri_returns_none_without_uri_or_default(self) -> None:
        self.assertIsNone(resolve_mqtt_uri(None, None))

    def test_main_errors_without_uri_or_default(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch('mqtty.mqtty.load_config_with_fallback', side_effect=FileNotFoundError('no config')),
            patch('mqtty.mqtty.sys.stderr', stderr),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main(['-l'])

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn('mqtt_uri is required unless MQTTY_URI or a bridge config is available', stderr.getvalue())

    def test_list_uses_mqtty_uri_default(self) -> None:
        stdout = io.StringIO()
        ports = [AvailableSerialPort('port-a', '/dev/ttyACM0')]
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench/test-host'}, clear=True),
            patch('mqtty.mqtty.socket.gethostname', return_value='test-host'),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports) as list_ports,
            patch('mqtty.mqtty.sys.stdout', stdout),
        ):
            main(['-l'])

        list_ports.assert_called_once_with('mqtt://broker.local/testbench/test-host', 1.0)
        self.assertEqual(stdout.getvalue(), 'mqtt://broker.local/testbench/test-host/port-a (/dev/ttyACM0)\n')

    def test_list_uses_serial_bridge_config_and_keeps_configured_scope_local(self) -> None:
        stdout = io.StringIO()
        config = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host='broker.local', port=1885, topic_base='testbench/serial'),
            usb_match=None,
        )
        ports = [
            AvailableSerialPort('port-a', '/dev/ttyUSB0'),
            AvailableSerialPort('remote-host/port-b', '/dev/ttyACM0'),
        ]
        with (
            patch.dict(os.environ, {}, clear=True),
            patch('mqtty.mqtty.load_config_with_fallback', return_value=(config, Path('/etc/mqtty-serial-bridge.toml'))),
            patch('mqtty.mqtty.socket.gethostname', return_value='some-other-host'),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports) as list_ports,
            patch('mqtty.mqtty.sys.stdout', stdout),
        ):
            main(['-l'])

        list_ports.assert_called_once_with('mqtt://broker.local:1885/testbench/serial', 1.0)
        self.assertEqual(stdout.getvalue(), 'mqtt://broker.local:1885/testbench/serial/port-a (/dev/ttyUSB0)\n')

    def test_list_all_includes_nested_hosts_below_bridge_config_scope(self) -> None:
        stdout = io.StringIO()
        config = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host='broker.local', port=1885, topic_base='testbench/serial'),
            usb_match=None,
        )
        ports = [
            AvailableSerialPort('port-a', '/dev/ttyUSB0'),
            AvailableSerialPort('remote-host/port-b', '/dev/ttyACM0'),
        ]
        with (
            patch.dict(os.environ, {}, clear=True),
            patch('mqtty.mqtty.load_config_with_fallback', return_value=(config, Path('/etc/mqtty-serial-bridge.toml'))),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports),
            patch('mqtty.mqtty.sys.stdout', stdout),
        ):
            main(['-la'])

        self.assertEqual(
            stdout.getvalue(),
            'mqtt://broker.local:1885/testbench/serial/port-a (/dev/ttyUSB0)\n'
            'mqtt://broker.local:1885/testbench/serial/remote-host/port-b (/dev/ttyACM0)\n',
        )

    def test_dev_path_uses_serial_bridge_config_for_discovery(self) -> None:
        config = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host='broker.local', port=1883, topic_base='testbench/serial'),
            usb_match=None,
        )
        ports = [
            AvailableSerialPort('port-a', '/dev/ttyUSB0'),
            AvailableSerialPort('remote-host/port-b', '/dev/ttyUSB0'),
        ]
        bridge = MagicMock()
        bridge.slave_name = None
        mqtty_cls = MagicMock(return_value=bridge)
        with (
            patch.dict(os.environ, {}, clear=True),
            patch('mqtty.mqtty.load_config_with_fallback', return_value=(config, Path('/etc/mqtty-serial-bridge.toml'))),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports) as list_ports,
            patch('mqtty.mqtty.MQTTY', mqtty_cls),
            patch('mqtty.mqtty.install_signal_handlers'),
        ):
            main(['/dev/ttyUSB0'])

        list_ports.assert_called_once_with('mqtt://broker.local:1883/testbench/serial', 1.0)
        mqtty_cls.assert_called_once_with('mqtt://broker.local:1883/testbench/serial/port-a', False)
        bridge.start_threads.assert_called_once_with()
        bridge.stdio_to_mqtt.assert_called_once_with()

    def test_list_explicit_uri_overrides_mqtty_uri_default(self) -> None:
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench/mark-desktop'}, clear=True),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=[]) as list_ports,
        ):
            main(['-l', 'mqtt://other.local/testbench/other-host'])

        list_ports.assert_called_once_with('mqtt://other.local/testbench/other-host', 1.0)

    def test_list_filters_to_local_hostname_by_default(self) -> None:
        stdout = io.StringIO()
        ports = [
            AvailableSerialPort('workstation-a/port-a', '/dev/ttyACM0'),
            AvailableSerialPort('workstation-b/port-b', '/dev/ttyUSB0'),
        ]
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench'}, clear=True),
            patch('mqtty.mqtty.socket.gethostname', return_value='workstation-a'),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports),
            patch('mqtty.mqtty.sys.stdout', stdout),
        ):
            main(['-l'])

        self.assertEqual(stdout.getvalue(), 'mqtt://broker.local/testbench/workstation-a/port-a (/dev/ttyACM0)\n')

    def test_list_all_includes_devices_from_other_hostnames(self) -> None:
        stdout = io.StringIO()
        ports = [
            AvailableSerialPort('workstation-a/port-a', '/dev/ttyACM0'),
            AvailableSerialPort('workstation-b/port-b', '/dev/ttyUSB0'),
        ]
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench'}, clear=True),
            patch('mqtty.mqtty.socket.gethostname', return_value='workstation-a'),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports),
            patch('mqtty.mqtty.sys.stdout', stdout),
        ):
            main(['-la'])

        self.assertEqual(
            stdout.getvalue(),
            'mqtt://broker.local/testbench/workstation-a/port-a (/dev/ttyACM0)\n'
            'mqtt://broker.local/testbench/workstation-b/port-b (/dev/ttyUSB0)\n',
        )

    def test_main_resolves_alias_before_connecting(self) -> None:
        ports = [AvailableSerialPort('pci-0000:12:00.3-usb-0:1.1.1.3:1.0', 'usb-c')]
        bridge = MagicMock()
        bridge.slave_name = None
        mqtty_cls = MagicMock(return_value=bridge)
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench/mark-desktop'}, clear=True),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports) as list_ports,
            patch('mqtty.mqtty.MQTTY', mqtty_cls),
            patch('mqtty.mqtty.install_signal_handlers'),
        ):
            main(['usb-c'])

        list_ports.assert_called_once_with('mqtt://broker.local/testbench/mark-desktop', 1.0)
        mqtty_cls.assert_called_once_with(
            'mqtt://broker.local/testbench/mark-desktop/pci-0000:12:00.3-usb-0:1.1.1.3:1.0',
            False,
        )
        bridge.start_threads.assert_called_once_with()
        bridge.stdio_to_mqtt.assert_called_once_with()

    def test_main_resolves_dev_path_alias_before_connecting(self) -> None:
        ports = [AvailableSerialPort('port-a', '/dev/ttyUSB0')]
        bridge = MagicMock()
        bridge.slave_name = None
        mqtty_cls = MagicMock(return_value=bridge)
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench/mark-desktop'}, clear=True),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=ports) as list_ports,
            patch('mqtty.mqtty.MQTTY', mqtty_cls),
            patch('mqtty.mqtty.install_signal_handlers'),
        ):
            main(['/dev/ttyUSB0'])

        list_ports.assert_called_once_with('mqtt://broker.local/testbench/mark-desktop', 1.0)
        mqtty_cls.assert_called_once_with('mqtt://broker.local/testbench/mark-desktop/port-a', False)
        bridge.start_threads.assert_called_once_with()
        bridge.stdio_to_mqtt.assert_called_once_with()

    def test_main_rejects_unknown_bare_alias_before_connecting(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {'MQTTY_URI': 'mqtt://broker.local/testbench/mark-desktop'}, clear=True),
            patch('mqtty.mqtty.list_available_serial_ports', return_value=[]),
            patch('mqtty.mqtty.MQTTY') as mqtty_cls,
            patch('mqtty.mqtty.sys.stderr', stderr),
        ):
            with self.assertRaises(SystemExit) as ctx:
                main(['foobar'])

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Unknown serial port or alias 'foobar'", stderr.getvalue())
        mqtty_cls.assert_not_called()

    def test_mqtt_connect_prints_initial_connection_notice_to_stderr(self) -> None:
        bridge = MQTTY('mqtt://broker.local/testbench/port-a', use_pty=False)
        stderr = io.StringIO()

        def connect_once(client: object, _connection: object) -> None:
            del client
            on_connect = cast(Any, bridge.mqtt_client.on_connect)
            on_subscribe = cast(Any, bridge.mqtt_client.on_subscribe)
            assert on_connect is not None
            assert on_subscribe is not None
            bridge.mqtt_client.subscribe = MagicMock(return_value=(0, 1))
            on_connect(bridge.mqtt_client, None, {}, 0, None)
            on_subscribe(bridge.mqtt_client, None, 1, [0], None)

        with (
            patch('mqtty.mqtty.connect_and_loop_forever', side_effect=connect_once),
            patch('mqtty.mqtty.sys.stderr', stderr),
        ):
            bridge.mqtt_connect()

        self.assertEqual(stderr.getvalue(), CONNECTION_NOTICE)
        self.assertTrue(bridge.connection_notice_printed)


if __name__ == '__main__':
    unittest.main()
