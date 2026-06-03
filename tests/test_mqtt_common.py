from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from mqtty.mqtt_common import MQTTConnectionInfo, connect_and_loop_forever


class MQTTCommonTests(unittest.TestCase):
    def test_connect_and_loop_forever_retries_first_connection(self) -> None:
        client = MagicMock()
        connection = MQTTConnectionInfo.parse("mqtt://broker.local/site/device")

        connect_and_loop_forever(client, connection)

        client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=30)
        client.connect_async.assert_called_once_with("broker.local", 1883)
        client.loop_forever.assert_called_once_with(retry_first_connection=True)


if __name__ == "__main__":
    unittest.main()
