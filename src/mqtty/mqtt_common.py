from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlparse

from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion

MQTTScheme = Literal["mqtt", "ws", "wss"]
MQTTTransport = Literal["tcp", "websockets"]
DISCOVERY_META_TOPIC = "_mqtty"
DISCOVERY_AVAILABLE_TOPIC = "available"

DEFAULT_PORTS: dict[MQTTScheme, int] = {
    "mqtt": 1883,
    "ws": 80,
    "wss": 443,
}


@dataclass(frozen=True)
class MQTTConnectionInfo:
    scheme: MQTTScheme
    host: str
    port: int
    transport: MQTTTransport
    base_path: str

    @classmethod
    def parse(cls, mqtt_uri: str) -> MQTTConnectionInfo:
        uri = urlparse(mqtt_uri)
        if uri.scheme not in DEFAULT_PORTS:
            raise ValueError("Invalid URI scheme. Expected 'mqtt://', 'ws://', or 'wss://'.")

        host = uri.hostname
        if host is None:
            raise ValueError("MQTT URI must include a hostname.")

        scheme = cast(MQTTScheme, uri.scheme)
        transport: MQTTTransport = "websockets" if scheme in {"ws", "wss"} else "tcp"
        return cls(
            scheme=scheme,
            host=host,
            port=uri.port or DEFAULT_PORTS[scheme],
            transport=transport,
            base_path=uri.path.strip("/"),
        )

    def topic(self, suffix: str) -> str:
        normalized_suffix = suffix.strip("/")
        if not self.base_path:
            return normalized_suffix
        if not normalized_suffix:
            return self.base_path
        return f"{self.base_path}/{normalized_suffix}"


def split_topic_path(topic: str) -> tuple[str, ...]:
    return tuple(part for part in topic.split("/") if part)


def join_topic_path(*parts: str) -> str:
    normalized_parts = [part.strip("/") for part in parts if part.strip("/")]
    return "/".join(normalized_parts)


def availability_topic(topic_base: str, port: str) -> str:
    return join_topic_path(topic_base, port, DISCOVERY_META_TOPIC, DISCOVERY_AVAILABLE_TOPIC)


def availability_subscribe_topic(topic_base: str) -> str:
    return join_topic_path(topic_base, "#")


def extract_available_path(topic: str, topic_base: str) -> str | None:
    topic_parts = split_topic_path(topic)
    base_parts = split_topic_path(topic_base)

    if len(topic_parts) < len(base_parts) + 3:
        return None
    if topic_parts[: len(base_parts)] != base_parts:
        return None
    if topic_parts[-2:] != (DISCOVERY_META_TOPIC, DISCOVERY_AVAILABLE_TOPIC):
        return None

    return join_topic_path(*topic_parts[len(base_parts) : -2])


def extract_available_port_name(topic: str, topic_base: str) -> str | None:
    available_path = extract_available_path(topic, topic_base)
    if available_path is None:
        return None

    return split_topic_path(available_path)[-1]


def create_client(connection: MQTTConnectionInfo) -> Client:
    return Client(
        transport=connection.transport,
        callback_api_version=CallbackAPIVersion.VERSION2,
    )


def connect_and_loop_forever(client: Client, connection: MQTTConnectionInfo) -> None:
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(connection.host, connection.port)
    client.loop_forever(retry_first_connection=True)
