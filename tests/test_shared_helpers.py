from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from mqtty.log_io import (
    FollowableSerialLogReader,
    SerialLogWriter,
    decode_serial_record,
    encode_serial_record,
    normalize_compressed_log_path,
    open_serial_log_reader,
)
from mqtty.mqtt_common import MQTTConnectionInfo


class MQTTConnectionInfoTests(unittest.TestCase):
    def test_parse_default_tcp_port(self) -> None:
        connection = MQTTConnectionInfo.parse("mqtt://broker.local/site/device")

        self.assertEqual(connection.host, "broker.local")
        self.assertEqual(connection.port, 1883)
        self.assertEqual(connection.transport, "tcp")
        self.assertEqual(connection.topic("device_serial_output"), "site/device/device_serial_output")

    def test_parse_default_websocket_port(self) -> None:
        connection = MQTTConnectionInfo.parse("wss://broker.local/site/device")

        self.assertEqual(connection.port, 443)
        self.assertEqual(connection.transport, "websockets")

    def test_topic_without_base_path(self) -> None:
        connection = MQTTConnectionInfo.parse("mqtt://broker.local")

        self.assertEqual(connection.topic("device_serial_input"), "device_serial_input")

    def test_invalid_scheme_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            MQTTConnectionInfo.parse("http://broker.local/site/device")


class LogIOTests(unittest.TestCase):
    def test_encode_decode_round_trip(self) -> None:
        delay_ms, payload = decode_serial_record(encode_serial_record(42, b"\x00hello"))

        self.assertEqual(delay_ms, 42.0)
        self.assertEqual(payload, b"\x00hello")

    def test_encode_includes_epoch_timestamp_first(self) -> None:
        line = encode_serial_record(42, b"hello", epoch_ms=1_746_123_456_789)
        self.assertTrue(line.startswith('{"ts":1746123456789,'))

        record = json.loads(line)
        self.assertEqual(record["ts"], 1_746_123_456_789)
        self.assertEqual(record["t"], 42)

    def test_decode_supports_legacy_records_without_timestamp(self) -> None:
        delay_ms, payload = decode_serial_record('{"t":7,"d":"aGVsbG8="}')

        self.assertEqual(delay_ms, 7.0)
        self.assertEqual(payload, b"hello")

    def test_replay_reader_supports_uncompressed_jsonl_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "plain.jsonl"
            log_path.write_text(f"{encode_serial_record(3, b'plain')}\n", encoding="utf-8")

            with open_serial_log_reader(log_path) as log_file:
                records = [decode_serial_record(line) for line in log_file]

        self.assertEqual(records, [(3.0, b"plain")])

    def test_log_writer_always_outputs_compressed_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "capture.jsonl"
            writer = SerialLogWriter(raw_path)
            try:
                writer.write_record(0, b"hello")
                writer.write_record(15, b"world")
            finally:
                writer.close()

            compressed_path = normalize_compressed_log_path(raw_path)
            self.assertTrue(compressed_path.exists())

            with open_serial_log_reader(compressed_path) as log_file:
                records = [decode_serial_record(line) for line in log_file]

        self.assertEqual(records, [(0.0, b"hello"), (15.0, b"world")])

    def test_log_writer_flushes_pending_data_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "capture.jsonl"
            compressed_path = normalize_compressed_log_path(raw_path)
            writer = SerialLogWriter(raw_path, flush_interval_s=0.05)
            try:
                writer.write_record(10, b"tail")
                time.sleep(0.06)
                writer.flush_if_due()

                with open_serial_log_reader(compressed_path) as log_file:
                    records = [decode_serial_record(line) for line in log_file]
            finally:
                writer.close()

        self.assertEqual(records, [(10.0, b"tail")])

    def test_followable_reader_resumes_compressed_logs_after_growth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "capture.jsonl"
            compressed_path = normalize_compressed_log_path(raw_path)
            writer = SerialLogWriter(raw_path, flush_interval_s=0.05)
            try:
                writer.write_record(0, b"first")

                with FollowableSerialLogReader(compressed_path) as log_file:
                    first = decode_serial_record(log_file.readline())
                    self.assertEqual(first, (0.0, b"first"))
                    self.assertEqual(log_file.readline(), "")

                    writer.write_record(10, b"second")
                    time.sleep(0.06)
                    writer.flush_if_due()

                    self.assertTrue(log_file.refresh_if_grown())
                    second = decode_serial_record(log_file.readline())
            finally:
                writer.close()

        self.assertEqual(second, (10.0, b"second"))

    def test_followable_reader_detects_growth_seen_before_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "capture.jsonl"
            compressed_path = normalize_compressed_log_path(raw_path)
            writer = SerialLogWriter(raw_path, flush_interval_s=0.05)
            try:
                writer.write_record(0, b"first")
                time.sleep(0.06)
                writer.flush_if_due()

                with FollowableSerialLogReader(compressed_path) as log_file:
                    first = decode_serial_record(log_file.readline())
                    self.assertEqual(first, (0.0, b"first"))
                    self.assertEqual(log_file.readline(), "")

                    writer.write_record(10, b"second")
                    time.sleep(0.06)
                    writer.flush_if_due()

                    # Old decompressor instance has already reached EOF.
                    self.assertEqual(log_file.readline(), "")
                    self.assertTrue(log_file.refresh_if_grown())
                    second = decode_serial_record(log_file.readline())
            finally:
                writer.close()

        self.assertEqual(second, (10.0, b"second"))


if __name__ == "__main__":
    unittest.main()
