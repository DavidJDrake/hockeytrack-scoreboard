"""MQTT connection to AWS IoT Core: TLS with the device certificate,
auto-reconnect, and routing of the three subscribed topics to callbacks.

The device's IoT policy only allows Connect, Subscribe and Receive --
this module must never publish. That holds even for the config topic, which
is inbound only: the admin site tells the device what to follow, and the
device never answers."""
from __future__ import annotations

import logging
import ssl
from pathlib import Path

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)
TODAY = "hockeytrack/games/today"


def _refused(reason_code) -> bool:
    """Did this CONNACK say no?

    paho's VERSION2 callbacks hand over a ReasonCode, which knows; older
    paths and MQTT v3 brokers can hand over a plain int, where 0 is the only
    success. Anything unrecognizable is treated as a refusal, because the
    cost of that is a help screen the owner can act on, while the cost of
    the opposite is a panel that never admits it is being turned away.
    """
    failure = getattr(reason_code, "is_failure", None)
    if failure is not None:
        return bool(failure)
    try:
        return int(reason_code) != 0
    except (TypeError, ValueError):
        return True


def config_topic(thing_name: str) -> str:
    """The device's own config topic. Scoped to one thing, and the IoT policy
    pins it to that thing via iot:Connection.Thing.ThingName, so a device
    cannot subscribe to anybody else's."""
    return f"scoreboard/{thing_name}/config"


class Link:
    def __init__(self, endpoint: str, client_id: str, cert: Path, key: Path, ca: Path,
                 on_state, on_today, on_link, on_config=None) -> None:
        self.on_state, self.on_today, self.on_link = on_state, on_today, on_link
        self.on_config = on_config
        self._config_topic = config_topic(client_id)
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
        # paho calls this for every CONNACK, including the ones that say no:
        # a certificate the broker will not accept, a policy that does not
        # allow this client id, a broker refusing the connection outright.
        # Reporting those as a working link is not cosmetic -- with the
        # reconnect backoff capped at 60 s it reset the "down since" clock
        # every minute, so the panel being turned away, which is exactly the
        # one that needs to say so, could never reach the "cannot reach the
        # service" screen.
        if _refused(reason_code):
            log.warning("connection refused: %s", reason_code)
            self.on_link(False)
            return
        log.info("connected: %s", reason_code)
        client.subscribe(TODAY, qos=1)
        # Retained, so a device that was unplugged when the game changed is
        # handed the current choice the moment it subscribes -- with the
        # RETAIN flag set, which is how main tells that replay apart from the
        # owner choosing something just now. See route().
        #
        # It is also why this client keeps paho's default clean_session=True.
        # With a persistent session the broker would queue QoS-1 publishes
        # made while the panel was away and deliver them on reconnect as
        # ordinary messages -- retain=0 -- and every one of those would read
        # as the owner choosing that game again. Make the session persistent
        # and main.config_action's fix stops working, silently.
        client.subscribe(self._config_topic, qos=1)
        if self._game is not None:
            client.subscribe(self._state_topic(self._game), qos=1)
        self.on_link(True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning("disconnected: %s", reason_code)
        self.on_link(False)

    def _on_message(self, client, userdata, msg):
        self.route(msg.topic, msg.payload, self.on_state, self.on_today,
                   self.on_config, self._config_topic, msg.retain)

    @staticmethod
    def route(topic: str, payload: bytes, on_state, on_today,
              on_config=None, config_topic_=None, retain: bool = False) -> None:
        """Dispatch one message. ``retain`` is the flag off the wire.

        It matters for exactly one topic. The config topic is published
        retained, and this device resubscribes to it on every reconnect, so
        the broker hands it the same choice again each time the link comes
        back. MQTT 3.1.1 3.3.1.3 is what makes those two cases separable: the
        server MUST set RETAIN=1 on a message sent because of a NEW
        subscription [MQTT-3.3.1-8] and MUST set RETAIN=0 on one sent because
        it matches an ESTABLISHED subscription, whatever flag the publisher
        used [MQTT-3.3.1-9]. So retain=1 here means "the broker replayed this
        when I subscribed" and retain=0 means "somebody published it just
        now" -- which is the difference between a reconnect and the owner
        pressing a button. main.config_action is where that is acted on.
        """
        if topic == TODAY:
            on_today(payload)
            return
        # Exact match rather than a pattern: the broker already guarantees we
        # only receive our own config, but matching the one topic we asked for
        # means a policy mistake cannot turn into someone else retargeting
        # this panel.
        if config_topic_ is not None and topic == config_topic_:
            if on_config is not None:
                on_config(payload, retain)
            return
        parts = topic.split("/")
        if len(parts) == 4 and parts[:2] == ["hockeytrack", "games"] and parts[3] == "state" and parts[2].isdigit():
            on_state(int(parts[2]), payload)

    @staticmethod
    def _state_topic(game_id: int) -> str:
        return f"hockeytrack/games/{game_id}/state"
