from __future__ import annotations

import argparse
import datetime as dt
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from paho.mqtt.client import Client, MQTTMessage
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from mqtty.log_io import SerialLogWriter
from mqtty.mqtt_common import MQTTConnectionInfo, connect_and_loop_forever, create_client

SERIAL_OUTPUT_TOPIC = "device_serial_output"
TopicKey = tuple[str, ...]


def split_topic_path(topic: str) -> tuple[str, ...]:
    return tuple(part for part in topic.split("/") if part)


def prefix_log_key(prefix_path: str, topic: str) -> TopicKey | None:
    prefix_parts = split_topic_path(prefix_path)
    topic_parts = split_topic_path(topic)

    if len(topic_parts) <= len(prefix_parts) or topic_parts[-1] != SERIAL_OUTPUT_TOPIC:
        return None
    if topic_parts[: len(prefix_parts)] != prefix_parts:
        return None

    relative_parts = topic_parts[len(prefix_parts) : -1]
    if not relative_parts:
        return None

    return relative_parts


def prefix_log_path(outdir: Path, key: TopicKey, date_str: str) -> Path:
    return outdir.joinpath(*key) / f"{date_str}_replay.jsonl.zst"


class BaseMQTTLogger:
    flush_interval_s = 0.25

    def __init__(self, mqtt_uri: str, subscribe_pattern: str) -> None:
        self.connection = MQTTConnectionInfo.parse(mqtt_uri)
        self.subscribe_topic = self.connection.topic(subscribe_pattern)
        self.client = create_client(self.connection)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self.stop_event = threading.Event()
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _on_connect(
        self,
        client: Client,
        _userdata: object,
        _flags: dict[str, Any],
        reason_code: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        if reason_code == 0:
            client.subscribe(self.subscribe_topic)
        else:
            sys.stderr.write(f"Connect failed rc={reason_code}\n")

    def _on_disconnect(
        self,
        _client: Client,
        _userdata: object,
        reason_code: ReasonCode | int | None,
        _properties: Properties | None,
        *_packet_from_broker: object,
    ) -> None:
        print(f"Disconnected rc={reason_code}", flush=True)

    def _signal_handler(self, signum: int, _frame: object) -> None:
        print(f"Received signal {signum}; shutting down...", file=sys.stderr)
        self.stop_event.set()

    def _on_message(self, _client: Client, _userdata: object, _msg: MQTTMessage) -> None:
        raise NotImplementedError

    def _flush_logs(self) -> None:
        return

    def run(self) -> None:
        try:
            threading.Thread(
                target=connect_and_loop_forever,
                args=(self.client, self.connection),
                daemon=True,
            ).start()
            print("Logging; press Ctrl+C to stop")
            while not self.stop_event.wait(self.flush_interval_s):
                self._flush_logs()
        finally:
            self._flush_logs()
            self.client.disconnect()


class MQTTSerialLogger(BaseMQTTLogger):
    def __init__(self, mqtt_uri: str, outfile: Path) -> None:
        super().__init__(mqtt_uri, SERIAL_OUTPUT_TOPIC)
        self.writer = SerialLogWriter(outfile)
        self.lock = threading.Lock()
        self.prev_time_ms: int | None = None

    def _on_message(self, _client: Client, _userdata: object, msg: MQTTMessage) -> None:
        now_ms = time.time_ns() // 1_000_000
        delay_ms = 0 if self.prev_time_ms is None else now_ms - self.prev_time_ms
        self.prev_time_ms = now_ms

        with self.lock:
            self.writer.write_record(delay_ms, msg.payload, epoch_ms=now_ms)

    def run(self) -> None:
        try:
            super().run()
        finally:
            self.writer.close()

    def _flush_logs(self) -> None:
        self.writer.flush_if_due()


class MQTTSerialPrefixLogger(BaseMQTTLogger):
    def __init__(self, mqtt_uri: str, outdir: Path) -> None:
        super().__init__(mqtt_uri, "#")
        self.outdir = outdir
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.writers: dict[TopicKey, SerialLogWriter] = {}
        self.prev_time_ms: dict[TopicKey, int | None] = {}
        self.current_date: dict[TopicKey, str] = {}
        self.lock = threading.Lock()

    def _on_message(self, _client: Client, _userdata: object, msg: MQTTMessage) -> None:
        key = prefix_log_key(self.connection.base_path, msg.topic)
        if key is None:
            return

        now_ms = time.time_ns() // 1_000_000
        date_str = dt.date.fromtimestamp(now_ms / 1000).isoformat()

        with self.lock:
            if key not in self.writers:
                self._open_log_writer(key, date_str)
            elif self.current_date[key] != date_str:
                self._rotate_log_writer(key, date_str)

            last_ms = self.prev_time_ms[key]
            delay_ms = 0 if last_ms is None else now_ms - last_ms
            self.prev_time_ms[key] = now_ms
            self.writers[key].write_record(delay_ms, msg.payload, epoch_ms=now_ms)

    def _log_path(self, key: TopicKey, date_str: str) -> Path:
        return prefix_log_path(self.outdir, key, date_str)

    def _open_log_writer(self, key: TopicKey, date_str: str) -> None:
        writer = SerialLogWriter(self._log_path(key, date_str))
        self.writers[key] = writer
        self.prev_time_ms[key] = None
        self.current_date[key] = date_str
        print(f"Logging {'/'.join(key)} -> {writer.path}")

    def _rotate_log_writer(self, key: TopicKey, new_date: str) -> None:
        self.writers[key].close()
        self._open_log_writer(key, new_date)

    def run(self) -> None:
        try:
            super().run()
        finally:
            for writer in self.writers.values():
                writer.close()

    def _flush_logs(self) -> None:
        with self.lock:
            writers = list(self.writers.values())

        for writer in writers:
            writer.flush_if_due()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Log MQTT serial output to replay files.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    device_parser = subparsers.add_parser(
        "device",
        help="Log one full device URL to a compressed replay file.",
    )
    device_parser.add_argument("mqtt_uri", help="Full device MQTT URI")
    device_parser.add_argument(
        "logfile",
        type=Path,
        help="Compressed replay file path (.zst is added if omitted)",
    )

    prefix_parser = subparsers.add_parser(
        "prefix",
        help="Log all descendant device output under an MQTT topic prefix.",
    )
    prefix_parser.add_argument("mqtt_uri", help="MQTT URI prefix above one or more devices")
    prefix_parser.add_argument(
        "outdir",
        type=Path,
        help="Base directory for rotated per-device replay logs",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        logger: BaseMQTTLogger
        if args.command == "device":
            logger = MQTTSerialLogger(args.mqtt_uri, args.logfile)
        elif args.command == "prefix":
            logger = MQTTSerialPrefixLogger(args.mqtt_uri, args.outdir)
        else:
            parser.error(f"Unknown command: {args.command}")

        logger.run()
    except ValueError as error:
        sys.stderr.write(f"Error: {error}\n")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
