from __future__ import annotations

import argparse
import contextlib
import json
import os
import pty
import select
import signal
import sys
import termios
import threading
import time
import tty
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Optional

from paho.mqtt.client import Client, MQTTMessage
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

from mqtty.mqtt_common import (
    MQTTConnectionInfo,
    availability_subscribe_topic,
    connect_and_loop_forever,
    create_client,
    extract_available_path,
)


PICOCOM_ESCAPE = 0x01
PICOCOM_EXIT = 0x18


@dataclass(frozen=True, slots=True)
class AvailableSerialPort:
    port: str
    alias: str


@contextlib.contextmanager
def raw_tty_mode(fd: int) -> Iterator[None]:
    if not os.isatty(fd):
        yield
        return

    original_mode = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original_mode)


class MQTTY:
    def __init__(self, mqtt_uri: str, use_pty: bool) -> None:
        self.connection = MQTTConnectionInfo.parse(mqtt_uri)
        self.use_pty = use_pty

        self.device_serial_input_topic = self.connection.topic("device_serial_input")
        self.device_serial_output_topic = self.connection.topic("device_serial_output")

        self.mqtt_client = create_client(self.connection)

        self.master_fd: Optional[int] = None
        self.slave_fd: Optional[int] = None
        self.slave_name: Optional[str] = None

        if self.use_pty:
            self.master_fd, self.slave_fd = pty.openpty()
            if self.slave_fd is not None:
                tty.setraw(self.slave_fd)
            self.slave_name = os.ttyname(self.slave_fd)

        self.connected = False
        self.connected_event = threading.Event()
        self.stop_event = threading.Event()
        self.escape_pending = False

    def on_message(self, _client: Client, _userdata: object, msg: MQTTMessage) -> None:
        try:
            if self.use_pty and self.master_fd is not None:
                os.write(self.master_fd, msg.payload)
            else:
                sys.stdout.buffer.write(msg.payload)
                sys.stdout.buffer.flush()
        except BrokenPipeError:
            self.shutdown()
        except Exception as e:
            sys.stderr.write(f"Error handling MQTT message: {e}\n")

    def mqtt_connect(self) -> None:
        self.mqtt_client.on_message = self.on_message

        def on_connect(
            client: Client,
            userdata: Any,
            flags: dict[str, Any],
            reason_code: ReasonCode,
            properties: Optional[Properties],
        ) -> None:
            del userdata, flags, properties
            if reason_code == 0:
                self.connected = True
                self.connected_event.set()
                client.subscribe(self.device_serial_output_topic)
            else:
                sys.stderr.write(f"Failed to connect to MQTT broker, return code {reason_code}\n")

        def on_disconnect(
            client: Client,
            userdata: Any,
            reason_code: ReasonCode | int | None,
            properties: Optional[Properties],
            *_packet_from_broker: object,
        ) -> None:
            del client, userdata, properties
            self.connected = False
            self.connected_event.clear()

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect

        try:
            connect_and_loop_forever(self.mqtt_client, self.connection)
        except Exception as e:
            sys.stderr.write(f"MQTT connection failed: {e}\n")
        finally:
            self.connected = False
            self.connected_event.clear()
            self.stop_event.set()

    def wait_for_connection(self) -> bool:
        while not self.stop_event.is_set():
            if self.connected_event.wait(timeout=0.1):
                return True

        return False

    def pty_to_mqtt(self) -> None:
        while not self.stop_event.is_set():
            if not self.wait_for_connection():
                break

            try:
                if self.master_fd is not None:
                    ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                    if self.master_fd in ready:
                        data = os.read(self.master_fd, 1024)
                        if not data:
                            break
                        if self.connected:
                            self.mqtt_client.publish(self.device_serial_input_topic, data)
            except Exception as e:
                if not self.stop_event.is_set():
                    sys.stderr.write(f"Error reading from PTY: {e}\n")
                break

        self.stop_event.set()

    def decode_stdin(self, data: bytes) -> tuple[bytes, bool]:
        output = bytearray()

        for byte in data:
            if self.escape_pending:
                self.escape_pending = False
                if byte == PICOCOM_EXIT:
                    return bytes(output), True
                if byte == PICOCOM_ESCAPE:
                    output.append(PICOCOM_ESCAPE)
                else:
                    output.extend((PICOCOM_ESCAPE, byte))
                continue

            if byte == PICOCOM_ESCAPE:
                self.escape_pending = True
            else:
                output.append(byte)

        return bytes(output), False

    def stdio_to_mqtt(self) -> None:
        stdin_fd = sys.stdin.fileno()
        use_escape_mode = os.isatty(stdin_fd)

        with raw_tty_mode(stdin_fd):
            while not self.stop_event.is_set():
                if not self.wait_for_connection():
                    break

                try:
                    ready, _, _ = select.select([stdin_fd], [], [], 0.1)
                except InterruptedError:
                    continue

                if stdin_fd not in ready:
                    continue

                data = os.read(stdin_fd, 1024)
                if not data:
                    break

                should_exit = False
                if use_escape_mode:
                    data, should_exit = self.decode_stdin(data)

                if data:
                    self.mqtt_client.publish(self.device_serial_input_topic, data)

                if should_exit:
                    break

        self.stop_event.set()

    def start_threads(self) -> None:
        mqtt_thread = threading.Thread(target=self.mqtt_connect, daemon=True)
        mqtt_thread.start()

        if self.use_pty:
            pty_thread = threading.Thread(target=self.pty_to_mqtt, daemon=True)
            pty_thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.connected = False
        self.connected_event.clear()
        with contextlib.suppress(Exception):
            self.mqtt_client.disconnect()
        if self.use_pty:
            if self.master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.master_fd)
                self.master_fd = None
            if self.slave_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self.slave_fd)
                self.slave_fd = None


def install_signal_handlers(bridge: MQTTY) -> None:
    def handle_signal(_signum: int, _frame: object) -> None:
        bridge.shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def available_port_from_message(topic: str, payload: bytes, topic_base: str) -> AvailableSerialPort | None:
    port = extract_available_path(topic, topic_base)
    if port is None:
        return None

    alias = port
    try:
        raw = json.loads(payload.decode("utf-8"))
        if isinstance(raw, dict):
            alias_raw = raw.get("alias")
            if alias_raw is not None:
                alias = str(alias_raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    return AvailableSerialPort(port=port, alias=alias)


def list_available_serial_ports(mqtt_uri: str, timeout_s: float) -> list[AvailableSerialPort]:
    connection = MQTTConnectionInfo.parse(mqtt_uri)
    client = create_client(connection)
    connected_event = threading.Event()
    ports: dict[str, AvailableSerialPort] = {}

    def on_connect(
        connected_client: Client,
        userdata: Any,
        flags: dict[str, Any],
        reason_code: ReasonCode,
        properties: Optional[Properties],
    ) -> None:
        del userdata, flags, properties
        if reason_code == 0:
            connected_client.subscribe(availability_subscribe_topic(connection.base_path))
            connected_event.set()
        else:
            raise RuntimeError(f"Failed to connect to MQTT broker, return code {reason_code}")

    def on_message(_client: Client, _userdata: object, msg: MQTTMessage) -> None:
        available_port = available_port_from_message(msg.topic, msg.payload, connection.base_path)
        if available_port is not None:
            ports[available_port.port] = available_port

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(connection.host, connection.port)
    client.loop_start()
    try:
        if not connected_event.wait(timeout=timeout_s):
            return []
        time.sleep(timeout_s)
    finally:
        client.loop_stop()
        client.disconnect()

    return [ports[port] for port in sorted(ports)]


def discovered_port_uri(base_uri: str, port: str) -> str:
    return f"{base_uri.rstrip('/')}/{port}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MQTTY: Bridge MQTT to a local terminal or PTY.")
    parser.add_argument("mqtt_uri", help="MQTT URI (e.g., mqtt://broker/topic)")
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="List retained mqtty serial ports under the MQTT URI and exit",
    )
    parser.add_argument(
        "--list-timeout",
        type=float,
        default=1.0,
        help="Seconds to wait for retained discovery messages when using --list",
    )
    parser.add_argument(
        "-p",
        "--pts-only",
        action="store_true",
        help="Only create and expose the PTS device; do not attach stdin/stdout",
    )
    args = parser.parse_args(argv)

    if args.list and args.pts_only:
        parser.error("--list cannot be combined with --pts-only")

    try:
        if args.list:
            for available_port in list_available_serial_ports(args.mqtt_uri, args.list_timeout):
                print(f"{discovered_port_uri(args.mqtt_uri, available_port.port)} ({available_port.alias})")
            return

        bridge = MQTTY(args.mqtt_uri, args.pts_only)
        install_signal_handlers(bridge)

        if args.pts_only and bridge.slave_name is not None:
            print(bridge.slave_name, flush=True)

        bridge.start_threads()

        if args.pts_only:
            while not bridge.stop_event.wait(timeout=1):
                pass
        else:
            bridge.stdio_to_mqtt()
    except ValueError as e:
        sys.stderr.write(f"Error: {e}\n")
        raise SystemExit(1) from e
    finally:
        if "bridge" in locals():
            bridge.shutdown()


if __name__ == "__main__":
    main()
