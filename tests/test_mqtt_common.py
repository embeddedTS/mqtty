from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from mqtty.mqtt_common import (
    MQTTConnectionInfo,
    connect_and_loop_forever,
    control_subscribe_topic,
    control_topic,
    extract_control_port_name,
    status_topic,
)


class MQTTCommonTests(unittest.TestCase):
    def test_control_and_status_topics(self) -> None:
        self.assertEqual(control_topic('testbench/serial', 'port-a'), 'testbench/serial/port-a/_mqtty/control')
        self.assertEqual(status_topic('testbench/serial', 'port-a'), 'testbench/serial/port-a/_mqtty/status')
        self.assertEqual(control_subscribe_topic('testbench/serial'), 'testbench/serial/+/_mqtty/control')

    def test_extract_control_port_name(self) -> None:
        self.assertEqual(
            extract_control_port_name('testbench/serial/port-a/_mqtty/control', 'testbench/serial'),
            'port-a',
        )
        self.assertIsNone(
            extract_control_port_name('testbench/serial/port-a/_mqtty/status', 'testbench/serial'),
        )

    def test_connect_and_loop_forever_retries_first_connection(self) -> None:
        client = MagicMock()
        connection = MQTTConnectionInfo.parse("mqtt://broker.local/site/device")

        connect_and_loop_forever(client, connection)

        client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=30)
        client.connect_async.assert_called_once_with("broker.local", 1883)
        client.loop_forever.assert_called_once_with(retry_first_connection=True)


if __name__ == "__main__":
    unittest.main()
