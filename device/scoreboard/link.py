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

    Three shapes, in order of how much they know. paho's VERSION2 callbacks
    hand over a ReasonCode, which answers outright. Failing that, its
    ``value`` is the CONNACK code, where 0 is the only success -- and note
    that this is NOT the same as int(reason_code): paho 2.1.0's ReasonCode
    defines no __int__, so int() on one raises TypeError, inside a paho
    callback, where nothing catches it (N-6). Last, a plain int, which MQTT
    v3 brokers and older paho paths can pass.

    Anything else -- a shape none of the three fit -- is treated as NOT a
    refusal, and said loudly in the journal.

    That is a deliberate reversal of what this shipped with. "Cannot tell"
    used to mean "refused", which sounds like the careful choice and is in
    fact the only answer here that bricks the panel: returning True skips
    every SUBSCRIBE, so no state, no today list and no config can ever
    arrive, and the panel shows "cannot reach the service" for ever over a
    link that is working. If a future paho dropped is_failure and changed
    the code's shape, that would have happened on every CONNACK, including
    every successful one. Failing open costs almost nothing by comparison: a
    CONNACK that really was a refusal is followed by the broker closing the
    socket, which fires _on_disconnect and reports the link down through the
    ordinary path, and subscribing on a connection that is about to close
    does no harm.

    What it does cost, stated rather than left to be discovered: in that same
    hypothetical case on_link(True) would fire on every refused attempt,
    resetting main's "down since" clock, so "cannot reach the service" would
    never appear -- B-5's original symptom, accepted here over a panel that
    can never subscribe to anything at all, and logged loudly every time so
    that the journal says which of the two is happening.
    """
    failure = getattr(reason_code, "is_failure", None)
    if failure is not None:
        return bool(failure)
    code = getattr(reason_code, "value", reason_code)
    try:
        return int(code) != 0
    except (TypeError, ValueError):
        log.warning("cannot tell whether the broker accepted this connection: %r (%s); "
                    "treating it as accepted -- a disconnect will say otherwise",
                    reason_code, reason_code)
        return False


def _suback_failed(code) -> bool:
    """Did one entry of a SUBACK say no? paho's VERSION2 on_subscribe hands
    a list of ReasonCode, one per topic filter, built from MQTT 3's granted
    QoS bytes, where 0x80 is the failure and is_failure already reads it
    that way. A plain int is read by the same rule (a granted QoS of 0 to 2
    is a yes), and a shape neither fits is not a refusal, as in _refused
    and for the same reason: the cost of a wrong yes here is a status topic
    asked for again on the next connect, and nothing more."""
    failure = getattr(code, "is_failure", None)
    if failure is not None:
        return bool(failure)
    try:
        return int(code) >= 0x80
    except (TypeError, ValueError):
        return False


def config_topic(thing_name: str) -> str:
    """The device's own config topic. Scoped to one thing, and the IoT policy
    pins it to that thing via iot:Connection.Thing.ThingName, so a device
    cannot subscribe to anybody else's."""
    return f"scoreboard/{thing_name}/config"


class Link:
    def __init__(self, endpoint: str, client_id: str, cert: Path, key: Path, ca: Path,
                 on_state, on_today, on_link, on_config=None, status_topics=()) -> None:
        self.on_state, self.on_today, self.on_link = on_state, on_today, on_link
        self.on_config = on_config
        self._config_topic = config_topic(client_id)
        # scoreboard/<thing>/status/running/<version> and its two siblings
        # (design 8.1): subscribed to, never published to, never received
        # from. AWS IoT's own subscription lifecycle event carries the topic
        # names to a rule, which is how the site learns a panel's version
        # without this policy ever gaining a Publish. Each names this thing
        # only, and the policy's topicfilter resource pins it there; the
        # topics themselves are computed by scoreboard.update from the build
        # file and the records, not by this module.
        self._status_topics = tuple(status_topics)
        # Status subscriptions awaiting their SUBACK, by message id, and
        # the ones the broker answered with a failure code. A refused
        # status topic is logged once and not asked for again while this
        # process lives: the policy is what has to change, and asking on
        # every reconnect would only put the same line in the journal.
        self._status_pending: dict[int, str] = {}
        self._status_refused: set[str] = set()
        self._game: int | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        self._client.tls_set(ca_certs=str(ca), certfile=str(cert), keyfile=str(key), tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_subscribe = self._on_subscribe
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
        # QoS 0 and after the topics that matter: nothing will ever arrive on
        # these, and a broker that answers one of them with a SUBACK failure
        # (a policy without the status/* filter) must not cost the panel its
        # state or config. That ordering is all this code can do, and it is
        # not the whole story: subscribe() is asynchronous, on_link(True)
        # below fires before any SUBACK, and a broker that chose to drop the
        # connection over an unauthorized SUBSCRIBE instead of answering it
        # would fire _on_disconnect after the panel had called itself
        # healthy, and the link would flap once a minute. The IoT policy's
        # status/* filter (SCO-69) therefore has to be applied before a
        # panel runs this code; README says so under Updates.
        for topic in self._status_topics:
            if topic in self._status_refused:
                continue
            _, mid = client.subscribe(topic, qos=0)
            if mid is not None:
                self._status_pending[mid] = topic
        self.on_link(True)

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties=None):
        # Only the status topics are judged here. The SUBACKs for today,
        # state and config carry the same codes, but a refusal of those is
        # a policy that has stopped the panel working, and the link report
        # is the wrong place to hide it: nothing arrives, main shows the
        # outage, the journal has the SUBACK line from paho.
        topic = self._status_pending.pop(mid, None)
        if topic is None:
            return
        if any(_suback_failed(code) for code in reason_codes):
            self._status_refused.add(topic)
            log.warning("the broker refused the status subscription %s (%s); not asking again until restart. "
                        "The IoT policy needs the status/* topic filter (SCO-69).",
                        topic, ", ".join(str(code) for code in reason_codes))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning("disconnected: %s", reason_code)
        self._status_pending.clear()
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
