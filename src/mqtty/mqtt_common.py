from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from paho.mqtt.client import Client
from paho.mqtt.enums import CallbackAPIVersion

MQTTScheme = Literal["mqtt", "ws", "wss"]
MQTTTransport = Literal["tcp", "websockets"]

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

        scheme = uri.scheme
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


def create_client(connection: MQTTConnectionInfo) -> Client:
    return Client(
        transport=connection.transport,
        callback_api_version=CallbackAPIVersion.VERSION2,
    )


def connect_and_loop_forever(client: Client, connection: MQTTConnectionInfo) -> None:
    client.connect_async(connection.host, connection.port)
    client.loop_forever()
