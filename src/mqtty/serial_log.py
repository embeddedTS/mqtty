from __future__ import annotations

import argparse
import datetime as dt
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from paho.mqtt.client import Client, MQTTMessage
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from mqtty.log_io import SerialLogWriter
from mqtty.mqtt_common import MQTTConnectionInfo, connect_and_loop_forever, create_client

DeviceKey = tuple[str, str]


class BaseMQTTLogger:
    def __init__(self, mqtt_uri: str) -> None:
        self.connection = MQTTConnectionInfo.parse(mqtt_uri)
        self.subscribe_topic = self.connection.topic("device_serial_output")
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

    def run(self) -> None:
        try:
            threading.Thread(
                target=connect_and_loop_forever,
                args=(self.client, self.connection),
                daemon=True,
            ).start()
            print("Logging; press Ctrl+C to stop")
            while not self.stop_event.is_set():
                time.sleep(0.2)
        finally:
            self.client.disconnect()


class MQTTSerialLogger(BaseMQTTLogger):
    def __init__(self, mqtt_uri: str, outfile: Path) -> None:
        super().__init__(mqtt_uri)
        self.writer = SerialLogWriter(outfile)
        self.lock = threading.Lock()
        self.prev_time_ms: int | None = None

    def _on_message(self, _client: Client, _userdata: object, msg: MQTTMessage) -> None:
        now_ms = time.time_ns() // 1_000_000
        delay_ms = 0 if self.prev_time_ms is None else now_ms - self.prev_time_ms
        self.prev_time_ms = now_ms

        with self.lock:
            self.writer.write_record(delay_ms, msg.payload)

    def run(self) -> None:
        try:
            super().run()
        finally:
            self.writer.close()


class MQTTSerialLogMulti(BaseMQTTLogger):
    def __init__(self, mqtt_uri: str, outdir: Path) -> None:
        super().__init__(mqtt_uri)
        self.outdir = outdir
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.writers: dict[DeviceKey, SerialLogWriter] = {}
        self.prev_time_ms: dict[DeviceKey, int | None] = {}
        self.current_date: dict[DeviceKey, str] = {}
        self.lock = threading.Lock()

    def _on_message(self, _client: Client, _userdata: object, msg: MQTTMessage) -> None:
        parts = msg.topic.split("/")
        try:
            server, device = parts[-3], parts[-2]
        except IndexError:
            sys.stderr.write(f"Unexpected topic format: {msg.topic}\n")
            return

        key = (server, device)
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
            self.writers[key].write_record(delay_ms, msg.payload)

    def _log_path(self, key: DeviceKey, date_str: str) -> Path:
        server, device = key
        return self.outdir / server / device / f"{date_str}_replay.jsonl.zst"

    def _open_log_writer(self, key: DeviceKey, date_str: str) -> None:
        writer = SerialLogWriter(self._log_path(key, date_str))
        self.writers[key] = writer
        self.prev_time_ms[key] = None
        self.current_date[key] = date_str
        print(f"Logging {key} -> {writer.path}")

    def _rotate_log_writer(self, key: DeviceKey, new_date: str) -> None:
        self.writers[key].close()
        self._open_log_writer(key, new_date)

    def run(self) -> None:
        try:
            super().run()
        finally:
            for writer in self.writers.values():
                writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Log MQTT serial output to replay files.")
    parser.add_argument("mqtt_uri", help="Base broker URI, for example mqtt://host/testbench/device")
    parser.add_argument(
        "--outfile",
        type=Path,
        help="Single compressed output file path (.zst is added if omitted)",
    )
    parser.add_argument("--outdir", type=Path, help="Base output directory for service mode")
    parser.add_argument("--service", action="store_true", help="Enable per-device log rotation mode")
    args = parser.parse_args()

    try:
        logger: BaseMQTTLogger
        if args.service:
            if args.outdir is None:
                parser.error("--outdir is required in service mode")
            logger = MQTTSerialLogMulti(args.mqtt_uri, args.outdir)
        else:
            if args.outfile is None:
                parser.error("--outfile is required unless --service is set")
            logger = MQTTSerialLogger(args.mqtt_uri, args.outfile)

        logger.run()
    except ValueError as error:
        sys.stderr.write(f"Error: {error}\n")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
