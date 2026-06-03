from __future__ import annotations

import unittest

from mqtty.mqtty import available_port_from_message, discovered_port_uri


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


if __name__ == '__main__':
    unittest.main()
