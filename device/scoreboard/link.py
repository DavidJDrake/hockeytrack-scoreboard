"""MQTT connection to AWS IoT Core: TLS with the device certificate,
auto-reconnect, and routing of the two topics to callbacks.

The device's IoT policy only allows Connect, Subscribe and Receive --
this module must never publish."""
from __future__ import annotations

import logging
import ssl
from pathlib import Path

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)
TODAY = "hockeytrack/games/today"


class Link:
    def __init__(self, endpoint: str, client_id: str, cert: Path, key: Path, ca: Path, on_state, on_today, on_link) -> None:
        self.on_state, self.on_today, self.on_link = on_state, on_today, on_link
        self._game: int | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        self._client.tls_set(ca_certs=str(ca), certfile=str(cert), keyfile=str(key), tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._endpoint = endpoint

    # --- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._client.connect_async(self._endpoint, 8883, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def follow(self, game_id: int | None) -> None:
        if self._game is not None and self._game != game_id:
            self._client.unsubscribe(self._state_topic(self._game))
        self._game = game_id
        if game_id is not None and self._client.is_connected():
            self._client.subscribe(self._state_topic(game_id), qos=1)

    # --- callbacks (paho-mqtt 2.x VERSION2 signatures) ------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("connected: %s", reason_code)
        client.subscribe(TODAY, qos=1)
        if self._game is not None:
            client.subscribe(self._state_topic(self._game), qos=1)
        self.on_link(True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning("disconnected: %s", reason_code)
        self.on_link(False)

    def _on_message(self, client, userdata, msg):
        self.route(msg.topic, msg.payload, self.on_state, self.on_today)

    @staticmethod
    def route(topic: str, payload: bytes, on_state, on_today) -> None:
        if topic == TODAY:
            on_today(payload)
            return
        parts = topic.split("/")
        if len(parts) == 4 and parts[:2] == ["hockeytrack", "games"] and parts[3] == "state" and parts[2].isdigit():
            on_state(int(parts[2]), payload)

    @staticmethod
    def _state_topic(game_id: int) -> str:
        return f"hockeytrack/games/{game_id}/state"
