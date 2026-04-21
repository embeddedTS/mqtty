from __future__ import annotations

import contextlib
import os
import pty
import queue
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import tty
import unittest
from pathlib import Path
from unittest.mock import patch

from paho.mqtt.client import Client, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from mqtty.log_io import decode_serial_record, normalize_compressed_log_path, open_serial_log_reader
from mqtty.mqtty import MQTTY
from mqtty.serial_bridge import (
    DEVICE_SERIAL_INPUT_TOPIC,
    DEVICE_SERIAL_OUTPUT_TOPIC,
    MQTTBridgeConfig,
    SerialBridge,
    SerialBridgeConfig,
    join_topic_path,
)
from mqtty.serial_log import MQTTSerialLogger


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_until(predicate: object, timeout_s: float, interval_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return True
        time.sleep(interval_s)
    return bool(callable(predicate) and predicate())


def read_exact(fd: int, size: int, timeout_s: float) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + timeout_s

    while len(data) < size and time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([fd], [], [], remaining)
        if not readable:
            continue
        chunk = os.read(fd, size - len(data))
        if not chunk:
            break
        data.extend(chunk)

    return bytes(data)


class MosquittoBroker:
    def __init__(self, binary_path: str) -> None:
        self.binary_path = binary_path
        self.port = reserve_local_port()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        config_path = Path(self._tmpdir.name) / "mosquitto.conf"
        config_path.write_text(
            "\n".join(
                [
                    f"listener {self.port} 127.0.0.1",
                    "allow_anonymous true",
                    "persistence false",
                    "log_dest stderr",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        self._process = subprocess.Popen(
            [self.binary_path, "-c", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("mosquitto exited before becoming ready")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.1)
                if sock.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)

        raise TimeoutError("timed out waiting for mosquitto to listen")

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3.0)

        self._tmpdir.cleanup()


class MQTTTestClient:
    def __init__(self, host: str, port: int) -> None:
        self._connected = threading.Event()
        self._messages: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self.client = Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(host, port)
        self.client.loop_start()

        if not self._connected.wait(timeout=5.0):
            self.close()
            raise TimeoutError("timed out waiting for MQTT client connection")

    def _on_connect(
        self,
        _client: Client,
        _userdata: object,
        _flags: dict[str, object],
        reason_code: object,
        _properties: object,
    ) -> None:
        if reason_code == 0:
            self._connected.set()

    def _on_message(self, _client: Client, _userdata: object, msg: MQTTMessage) -> None:
        self._messages.put((msg.topic, msg.payload))

    def subscribe(self, topic: str) -> None:
        result, _mid = self.client.subscribe(topic)
        if result != 0:
            raise RuntimeError(f"subscribe failed with rc={result}")

    def publish(self, topic: str, payload: bytes) -> None:
        info = self.client.publish(topic, payload)
        info.wait_for_publish(timeout=5.0)

    def wait_for_message(self, topic: str, timeout_s: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                seen_topic, payload = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"timed out waiting for topic {topic!r}") from exc
            if seen_topic == topic:
                return payload

        raise TimeoutError(f"timed out waiting for topic {topic!r}")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.client.disconnect()
        with contextlib.suppress(Exception):
            self.client.loop_stop()


class MQTTAppsIntegrationTests(unittest.TestCase):
    broker: MosquittoBroker

    @classmethod
    def setUpClass(cls) -> None:
        mosquitto_path = shutil.which("mosquitto")
        if mosquitto_path is None:
            raise AssertionError("mosquitto is required for integration tests but was not found on PATH")

        cls.broker = MosquittoBroker(mosquitto_path)
        cls.broker.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.broker.stop()

    def mqtt_uri(self, base_path: str) -> str:
        return f"mqtt://127.0.0.1:{self.broker.port}/{base_path}"

    def test_mqtty_pts_round_trip_with_real_broker(self) -> None:
        bridge = MQTTY(self.mqtt_uri("itest/mqtty/device-a"), use_pty=True)
        self.addCleanup(bridge.shutdown)

        bridge.start_threads()
        self.assertTrue(bridge.connected_event.wait(timeout=5.0))
        self.assertIsNotNone(bridge.slave_name)

        slave_fd = os.open(str(bridge.slave_name), os.O_RDWR | os.O_NOCTTY)
        self.addCleanup(lambda: os.close(slave_fd))
        tty.setraw(slave_fd)

        subscriber = MQTTTestClient("127.0.0.1", self.broker.port)
        self.addCleanup(subscriber.close)
        subscriber.subscribe(bridge.device_serial_input_topic)

        publisher = MQTTTestClient("127.0.0.1", self.broker.port)
        self.addCleanup(publisher.close)

        outbound = b"pty-to-mqtt"
        os.write(slave_fd, outbound)
        self.assertEqual(subscriber.wait_for_message(bridge.device_serial_input_topic), outbound)

        inbound = b"mqtt-to-pty"
        publisher.publish(bridge.device_serial_output_topic, inbound)
        self.assertEqual(read_exact(slave_fd, len(inbound), timeout_s=5.0), inbound)

    def test_serial_bridge_round_trip_with_fake_serial_symlink(self) -> None:
        cfg = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host="127.0.0.1", port=self.broker.port, topic_base="itest/serial-bridge"),
            usb_match=None,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            serial_base = Path(temp_dir)
            port_name = "platform-ci_hdrc.1-usb-0:1.2:1.0"

            master_fd, slave_fd = pty.openpty()
            self.addCleanup(lambda: os.close(master_fd))
            self.addCleanup(lambda: os.close(slave_fd))
            tty.setraw(slave_fd)

            slave_path = os.ttyname(slave_fd)
            os.symlink(slave_path, serial_base / port_name)

            bridge = SerialBridge(cfg, serial_base_path=serial_base, scan_interval_s=0.05)

            patcher = patch("mqtty.serial_bridge.vid_pid_from_symlink", return_value=("1a86", "7523"))
            patcher.start()
            self.addCleanup(patcher.stop)

            bridge_thread = threading.Thread(target=bridge.run, daemon=True)
            bridge_thread.start()

            def stop_bridge() -> None:
                bridge.stop()
                bridge_thread.join(timeout=5.0)

            self.addCleanup(stop_bridge)

            self.assertTrue(wait_until(lambda: port_name in bridge.serial_ports, timeout_s=5.0))

            output_topic = join_topic_path(cfg.mqtt.topic_base, port_name, DEVICE_SERIAL_OUTPUT_TOPIC)
            input_topic = join_topic_path(cfg.mqtt.topic_base, port_name, DEVICE_SERIAL_INPUT_TOPIC)

            subscriber = MQTTTestClient("127.0.0.1", self.broker.port)
            self.addCleanup(subscriber.close)
            subscriber.subscribe(output_topic)

            publisher = MQTTTestClient("127.0.0.1", self.broker.port)
            self.addCleanup(publisher.close)

            serial_to_mqtt = b"serial-output"
            os.write(master_fd, serial_to_mqtt)
            self.assertEqual(subscriber.wait_for_message(output_topic), serial_to_mqtt)

            mqtt_to_serial = b"serial-input"
            publisher.publish(input_topic, mqtt_to_serial)
            self.assertEqual(read_exact(master_fd, len(mqtt_to_serial), timeout_s=5.0), mqtt_to_serial)

            # Simulate unplug/replug of the by-path symlink and verify the bridge recovers.
            os.unlink(serial_base / port_name)
            self.assertTrue(wait_until(lambda: port_name not in bridge.serial_ports, timeout_s=5.0))

            os.symlink(slave_path, serial_base / port_name)
            self.assertTrue(wait_until(lambda: port_name in bridge.serial_ports, timeout_s=5.0))

            serial_to_mqtt_again = b"serial-output-2"
            os.write(master_fd, serial_to_mqtt_again)
            self.assertEqual(subscriber.wait_for_message(output_topic), serial_to_mqtt_again)

    def test_serial_bridge_honors_usb_allow_list(self) -> None:
        cfg = SerialBridgeConfig(
            mqtt=MQTTBridgeConfig(host="127.0.0.1", port=self.broker.port, topic_base="itest/serial-bridge-filter"),
            usb_match=(("0403", "6001"),),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            serial_base = Path(temp_dir)
            port_name = "platform-ci_hdrc.1-usb-0:1.3:1.0"

            master_fd, slave_fd = pty.openpty()
            self.addCleanup(lambda: os.close(master_fd))
            self.addCleanup(lambda: os.close(slave_fd))
            tty.setraw(slave_fd)

            slave_path = os.ttyname(slave_fd)
            os.symlink(slave_path, serial_base / port_name)

            bridge = SerialBridge(cfg, serial_base_path=serial_base, scan_interval_s=0.05)
            bridge_thread = threading.Thread(target=bridge.run, daemon=True)

            patcher = patch("mqtty.serial_bridge.vid_pid_from_symlink", return_value=("1a86", "7523"))
            patcher.start()
            self.addCleanup(patcher.stop)

            bridge_thread.start()

            def stop_bridge() -> None:
                bridge.stop()
                bridge_thread.join(timeout=5.0)

            self.addCleanup(stop_bridge)

            self.assertTrue(wait_until(lambda: port_name in bridge.ignored_ports, timeout_s=5.0))
            self.assertNotIn(port_name, bridge.serial_ports)

    def test_serial_logger_prefix_writes_per_device_logs(self) -> None:
        from mqtty.serial_log import MQTTSerialPrefixLogger

        with tempfile.TemporaryDirectory() as temp_dir:
            outdir = Path(temp_dir) / "logs"
            logger = MQTTSerialPrefixLogger(self.mqtt_uri("itest/prefix"), outdir)
            subscribed = threading.Event()

            def on_subscribe(
                _client: Client,
                _userdata: object,
                _mid: int,
                _reason_codes: list[ReasonCode],
                _properties: Properties,
            ) -> None:
                subscribed.set()

            logger.client.on_subscribe = on_subscribe
            logger_thread = threading.Thread(target=logger.run, daemon=True)
            logger_thread.start()

            def stop_logger() -> None:
                logger.stop_event.set()
                logger_thread.join(timeout=5.0)

            self.addCleanup(stop_logger)

            self.assertTrue(wait_until(logger.client.is_connected, timeout_s=5.0))
            self.assertTrue(subscribed.wait(timeout=5.0))
            time.sleep(0.1)

            publisher = MQTTTestClient("127.0.0.1", self.broker.port)
            self.addCleanup(publisher.close)

            topic_a = "itest/prefix/server-a/port-a/device_serial_output"
            topic_b = "itest/prefix/server-b/port-b/device_serial_output"
            publisher.publish(topic_a, b"a1")
            publisher.publish(topic_b, b"b1")
            time.sleep(0.3)

            logger.stop_event.set()
            logger_thread.join(timeout=5.0)

            files = sorted(outdir.rglob("*_replay.jsonl.zst"))
            self.assertEqual(len(files), 2)

            path_a = outdir / "server-a" / "port-a"
            path_b = outdir / "server-b" / "port-b"
            self.assertEqual(sum(1 for path in files if path.parent == path_a), 1)
            self.assertEqual(sum(1 for path in files if path.parent == path_b), 1)

            payloads_by_parent: dict[Path, list[bytes]] = {}
            for path in files:
                with open_serial_log_reader(path) as log_file:
                    payloads_by_parent[path.parent] = [decode_serial_record(line)[1] for line in log_file]

            self.assertIn(b"a1", payloads_by_parent[path_a])
            self.assertIn(b"b1", payloads_by_parent[path_b])

    def test_serial_logger_writes_records_from_real_broker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_log_path = Path(temp_dir) / "capture.jsonl"
            logger = MQTTSerialLogger(self.mqtt_uri("itest/logging/device-b"), raw_log_path)
            subscribed = threading.Event()

            def on_subscribe(
                _client: Client,
                _userdata: object,
                _mid: int,
                _reason_codes: list[ReasonCode],
                _properties: Properties,
            ) -> None:
                subscribed.set()

            logger.client.on_subscribe = on_subscribe

            logger_thread = threading.Thread(target=logger.run, daemon=True)
            logger_thread.start()

            def stop_logger() -> None:
                logger.stop_event.set()
                logger_thread.join(timeout=5.0)

            self.addCleanup(stop_logger)

            self.assertTrue(wait_until(logger.client.is_connected, timeout_s=5.0))
            self.assertTrue(subscribed.wait(timeout=5.0))
            time.sleep(0.1)

            publisher = MQTTTestClient("127.0.0.1", self.broker.port)
            self.addCleanup(publisher.close)

            topic = logger.connection.topic(DEVICE_SERIAL_OUTPUT_TOPIC)
            publisher.publish(topic, b"first")
            time.sleep(0.1)
            publisher.publish(topic, b"second")
            time.sleep(0.5)

            logger.stop_event.set()
            logger_thread.join(timeout=5.0)

            compressed_path = normalize_compressed_log_path(raw_log_path)
            self.assertTrue(compressed_path.exists())

            with open_serial_log_reader(compressed_path) as log_file:
                payloads = [decode_serial_record(line)[1] for line in log_file]

            self.assertIn(b"first", payloads)
            self.assertIn(b"second", payloads)


if __name__ == "__main__":
    unittest.main()
