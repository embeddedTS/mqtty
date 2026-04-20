from __future__ import annotations

import argparse
import logging
import os
import select
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from paho.mqtt.client import Client, MQTTMessage
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.properties import Properties
from paho.mqtt.reasoncodes import ReasonCode

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - python <3.11
    import tomli as tomllib  # type: ignore[import-not-found]

DEFAULT_CONFIG_PATHS = [
    Path('/etc/mqtty-serial-bridge.toml'),
    Path('/etc/uart2mqtt.toml'),
]
DEFAULT_SERIAL_BASE_PATH = Path('/dev/serial/by-path')
DEFAULT_BAUD_RATE = 115200
DEFAULT_SCAN_INTERVAL_S = 1.0

DEVICE_SERIAL_INPUT_TOPIC = 'device_serial_input'
DEVICE_SERIAL_OUTPUT_TOPIC = 'device_serial_output'

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MQTTBridgeConfig:
    host: str
    port: int
    topic_base: str


@dataclass(frozen=True, slots=True)
class SerialBridgeConfig:
    mqtt: MQTTBridgeConfig
    usb_match: tuple[tuple[str, str], ...] | None


@dataclass(slots=True)
class SerialPortState:
    connection: Any
    thread: threading.Thread
    serial_output_topic: str
    real_device_path: str


def split_topic_path(topic: str) -> tuple[str, ...]:
    return tuple(part for part in topic.split('/') if part)


def join_topic_path(*parts: str) -> str:
    normalized_parts = [part.strip('/') for part in parts if part.strip('/')]
    return '/'.join(normalized_parts)


def extract_port_name(topic: str, topic_base: str) -> str | None:
    topic_parts = split_topic_path(topic)
    base_parts = split_topic_path(topic_base)

    if len(topic_parts) != len(base_parts) + 2:
        return None
    if topic_parts[: len(base_parts)] != base_parts:
        return None
    if topic_parts[-1] != DEVICE_SERIAL_INPUT_TOPIC:
        return None

    return topic_parts[-2]


def load_config(path: Path) -> SerialBridgeConfig:
    if not path.exists():
        raise FileNotFoundError(path)

    with path.open('rb') as file:
        raw = tomllib.load(file)

    mqtt_raw = raw.get('mqtt', {})
    mqtt_cfg = MQTTBridgeConfig(
        host=str(mqtt_raw.get('host', 'localhost')),
        port=int(mqtt_raw.get('port', 1883)),
        topic_base=join_topic_path(str(mqtt_raw.get('topic_base', 'testbench'))),
    )

    usb_match_raw = raw.get('usb_match')
    usb_match: tuple[tuple[str, str], ...] | None = None

    if usb_match_raw:
        allow_list: list[tuple[str, str]] = []
        for entry in usb_match_raw:
            vid = str(entry.get('vid', '*')).lower()
            pid = str(entry.get('pid', '*')).lower()
            allow_list.append((vid, pid))
        usb_match = tuple(allow_list)

    return SerialBridgeConfig(mqtt=mqtt_cfg, usb_match=usb_match)


def load_config_with_fallback(path: Path | None) -> tuple[SerialBridgeConfig, Path]:
    if path is not None:
        return load_config(path), path

    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.exists():
            return load_config(candidate), candidate

    searched_paths = ', '.join(str(candidate) for candidate in DEFAULT_CONFIG_PATHS)
    raise FileNotFoundError(f'No config file found. Checked: {searched_paths}')


def vid_pid_from_symlink(symlink: Path) -> tuple[str, str] | None:
    try:
        real_tty = Path(os.path.realpath(symlink))
        tty_name = real_tty.name
        sys_tty = Path('/sys/class/tty') / tty_name / 'device'
        dev_path = sys_tty.resolve()

        # Some USB adapters expose VID/PID a couple of parents up the path.
        for _ in range(3):
            vid_file = dev_path / 'idVendor'
            pid_file = dev_path / 'idProduct'
            if vid_file.exists() and pid_file.exists():
                vid = vid_file.read_text(encoding='utf-8').strip().lower()
                pid = pid_file.read_text(encoding='utf-8').strip().lower()
                return vid, pid
            dev_path = dev_path.parent

        logger.warning('No VID:PID found for %s after walking sysfs', symlink)
        return None
    except Exception as exc:  # pragma: no cover - depends on host sysfs layout
        logger.warning('Failed to resolve VID:PID for %s: %s', symlink, exc)
        return None


def usb_allowed(vid: str, pid: str, allow: tuple[tuple[str, str], ...] | None) -> bool:
    if allow is None:
        return True

    for allow_vid, allow_pid in allow:
        if allow_vid in {'*', vid} and allow_pid in {'*', pid}:
            return True
    return False


class SerialBridge:
    def __init__(
        self,
        cfg: SerialBridgeConfig,
        serial_base_path: Path = DEFAULT_SERIAL_BASE_PATH,
        baud_rate: int = DEFAULT_BAUD_RATE,
        scan_interval_s: float = DEFAULT_SCAN_INTERVAL_S,
    ) -> None:
        self.cfg = cfg
        self.serial_base_path = serial_base_path
        self.baud_rate = baud_rate
        self.scan_interval_s = scan_interval_s

        self.mqtt_client = Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self.serial_ports: dict[str, SerialPortState] = {}
        self.ignored_ports: set[str] = set()
        self.opened_real_devices: set[str] = set()
        self.stop_event = threading.Event()
        self._state_lock = threading.Lock()

    @property
    def input_subscribe_topic(self) -> str:
        return join_topic_path(self.cfg.mqtt.topic_base, '+', DEVICE_SERIAL_INPUT_TOPIC)

    def mqtt_connect(self) -> None:
        def on_connect(
            client: Client,
            _userdata: Any,
            _flags: dict[str, Any],
            reason_code: ReasonCode,
            _properties: Properties | None,
        ) -> None:
            if reason_code == 0:
                logger.info('Connected to MQTT broker')
                client.subscribe(self.input_subscribe_topic)
            else:
                logger.error('MQTT connection failed with code %s', reason_code)

        def on_disconnect(
            _client: Client,
            _userdata: Any,
            reason_code: ReasonCode | int | None,
            _properties: Properties | None,
            *_packet_from_broker: object,
        ) -> None:
            logger.warning('Disconnected from MQTT broker (rc=%s)', reason_code)

        def on_message(
            _client: Client,
            _userdata: Any,
            message: MQTTMessage,
        ) -> None:
            port_name = extract_port_name(message.topic, self.cfg.mqtt.topic_base)
            if port_name is None:
                return

            with self._state_lock:
                state = self.serial_ports.get(port_name)

            if state is None:
                logger.debug('Port %s not found for topic %s', port_name, message.topic)
                return

            try:
                state.connection.write(message.payload)
            except Exception as exc:
                logger.error('Error writing to UART %s: %s', port_name, exc)

        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect
        self.mqtt_client.on_message = on_message

        while not self.stop_event.is_set():
            try:
                self.mqtt_client.connect(self.cfg.mqtt.host, self.cfg.mqtt.port)
                self.mqtt_client.loop_start()
                return
            except Exception as exc:
                logger.error('Failed to connect to MQTT broker: %s, retrying...', exc)
                time.sleep(3)

    def monitor_serial_ports(self) -> None:
        while not self.stop_event.is_set():
            try:
                available_ports = self._list_candidate_ports()
                with self._state_lock:
                    current_ports = set(self.serial_ports.keys())
                    ignored_ports = set(self.ignored_ports)

                new_ports = available_ports - current_ports - ignored_ports

                for port in new_ports:
                    self._handle_new_port(port)

                for port in current_ports - available_ports:
                    logger.info('Serial port removed: %s', port)
                    self.stop_serial_thread(port)

                with self._state_lock:
                    for port in list(self.ignored_ports):
                        if port not in available_ports:
                            self.ignored_ports.remove(port)

                time.sleep(self.scan_interval_s)
            except FileNotFoundError as exc:
                if exc.filename == str(self.serial_base_path):
                    logger.warning('%s not found, retrying in %.1f second', self.serial_base_path, self.scan_interval_s)
                else:
                    logger.error('Unexpected FileNotFoundError: %s', exc)
                time.sleep(self.scan_interval_s)
            except Exception as exc:
                logger.error('Error monitoring serial ports: %s', exc)

    def _list_candidate_ports(self) -> set[str]:
        ports: set[str] = set()
        for item in os.listdir(self.serial_base_path):
            if 'usb' in item and 'usbv2' in item:
                continue
            ports.add(item)
        return ports

    def _handle_new_port(self, port: str) -> None:
        full_path = self.serial_base_path / port
        real_dev_path = os.path.realpath(full_path)

        with self._state_lock:
            if real_dev_path in self.opened_real_devices:
                logger.debug('Skipping duplicate symlink %s -> %s', port, real_dev_path)
                self.ignored_ports.add(port)
                return

        vid_pid = vid_pid_from_symlink(full_path)
        if vid_pid is None:
            logger.warning('Could not resolve VID:PID for %s, ignoring', port)
            with self._state_lock:
                self.ignored_ports.add(port)
            return

        vid, pid = vid_pid
        if not usb_allowed(vid, pid, self.cfg.usb_match):
            logger.info('Ignoring Serial Port <%s:%s>: %s', vid, pid, port)
            with self._state_lock:
                self.ignored_ports.add(port)
            return

        logger.info('Opening Serial Port <%s:%s>(%s): %s', vid, pid, real_dev_path, port)
        with self._state_lock:
            self.opened_real_devices.add(real_dev_path)

        self.start_serial_thread(port, real_dev_path)

    def start_serial_thread(self, port: str, real_dev_path: str | None = None) -> None:
        try:
            import serial

            full_path = self.serial_base_path / port
            serial_conn = serial.Serial(str(full_path), self.baud_rate, timeout=0)
            serial_output_topic = join_topic_path(self.cfg.mqtt.topic_base, port, DEVICE_SERIAL_OUTPUT_TOPIC)
            thread = threading.Thread(
                target=self.handle_serial,
                args=(port, serial_conn, serial_output_topic),
                daemon=True,
            )

            state = SerialPortState(
                connection=serial_conn,
                thread=thread,
                serial_output_topic=serial_output_topic,
                real_device_path=real_dev_path or os.path.realpath(full_path),
            )
            with self._state_lock:
                self.serial_ports[port] = state
            thread.start()
        except Exception as exc:
            logger.error('Failed to open %s: %s', port, exc)
            if real_dev_path:
                with self._state_lock:
                    self.opened_real_devices.discard(real_dev_path)

    def stop_serial_thread(self, port: str) -> None:
        with self._state_lock:
            state = self.serial_ports.get(port)

        if state is None:
            return

        try:
            state.thread.join(timeout=2.0)
            state.connection.close()
            logger.info('Stopped monitoring serial port: %s', port)
        except Exception as exc:
            logger.error('Error stopping %s: %s', port, exc)
        finally:
            with self._state_lock:
                removed_state = self.serial_ports.pop(port, None)
                if removed_state is not None:
                    self.opened_real_devices.discard(removed_state.real_device_path)

    def handle_serial(self, port: str, serial_conn: Any, serial_output_topic: str) -> None:
        try:
            while not self.stop_event.is_set():
                readable, _, _ = select.select([serial_conn], [], [], 0.1)
                if not readable:
                    continue

                data = serial_conn.read(serial_conn.in_waiting or 1)
                if data:
                    self.mqtt_client.publish(serial_output_topic, data)
        except Exception as exc:
            logger.error('Error in thread for %s: %s', port, exc)
        finally:
            try:
                serial_conn.close()
            except Exception:
                pass

            with self._state_lock:
                state = self.serial_ports.pop(port, None)
                if state is not None:
                    self.opened_real_devices.discard(state.real_device_path)

            logger.info('Thread exiting for port: %s', port)

    def stop(self) -> None:
        self.stop_event.set()
        with self._state_lock:
            ports = list(self.serial_ports.keys())

        for port in ports:
            self.stop_serial_thread(port)

        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

    def run(self) -> None:
        self.mqtt_connect()

        try:
            monitor_thread = threading.Thread(target=self.monitor_serial_ports, daemon=True)
            monitor_thread.start()

            while not self.stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info('Shutting down...')
        finally:
            self.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Bridge local UART devices to MQTT topics.')
    parser.add_argument(
        '--config',
        type=Path,
        default=None,
        help='Path to config TOML file (defaults: /etc/mqtty-serial-bridge.toml then /etc/uart2mqtt.toml)',
    )
    parser.add_argument(
        '--serial-base-path',
        type=Path,
        default=DEFAULT_SERIAL_BASE_PATH,
        help='Directory containing serial by-path symlinks',
    )
    parser.add_argument('--baud-rate', type=int, default=DEFAULT_BAUD_RATE, help='UART baud rate')
    parser.add_argument('--scan-interval', type=float, default=DEFAULT_SCAN_INTERVAL_S, help='Port rescan interval seconds')
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        help='Logging verbosity',
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=getattr(logging, args.log_level), format='[%(levelname)s] %(message)s')

    try:
        cfg, config_path = load_config_with_fallback(args.config)
    except Exception as exc:
        logger.error('Failed to load config: %s', exc)
        return 1

    logger.info('Loaded config: %s', config_path)
    logger.info('topic_base = %r', cfg.mqtt.topic_base)

    bridge = SerialBridge(
        cfg,
        serial_base_path=args.serial_base_path,
        baud_rate=args.baud_rate,
        scan_interval_s=args.scan_interval,
    )
    bridge.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
