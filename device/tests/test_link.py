from scoreboard.link import Link, config_topic


def test_route_dispatches_by_topic():
    got = {}
    Link.route("hockeytrack/games/2026020001/state", b'{"v":1,"gameId":2026020001}', on_state=lambda gid, b: got.setdefault("state", (gid, b)), on_today=lambda b: got.setdefault("today", b))
    Link.route("hockeytrack/games/today", b'{"games":[]}', on_state=lambda gid, b: got.setdefault("state", (gid, b)), on_today=lambda b: got.setdefault("today", b))
    Link.route("hockeytrack/other", b"x", on_state=lambda *a: got.setdefault("bad", a), on_today=lambda *a: got.setdefault("bad", a))
    assert got["state"] == (2026020001, b'{"v":1,"gameId":2026020001}')
    assert got["today"] == b'{"games":[]}'
    assert "bad" not in got


def test_route_dispatches_the_devices_own_config_topic():
    """The admin site retargets a panel by publishing to its config topic.
    Routing is an exact match on the topic this device subscribed to, so a
    message meant for another panel is ignored even if one ever arrived."""
    mine = config_topic("scoreboard-abc123")
    got = {}
    sink = lambda *a: got.setdefault("bad", a)

    Link.route(mine, b'{"gameId":2026020001}', on_state=sink, on_today=sink,
               on_config=lambda b, retain: got.setdefault("config", b), config_topic_=mine)
    assert got["config"] == b'{"gameId":2026020001}'
    assert "bad" not in got

    # Another device's config topic must not dispatch here.
    other = config_topic("scoreboard-someone-else")
    got2 = {}
    Link.route(other, b'{"gameId":1}', on_state=sink, on_today=sink,
               on_config=lambda b, retain: got2.setdefault("config", b), config_topic_=mine)
    assert got2 == {}


def test_config_topic_is_scoped_to_one_thing():
    assert config_topic("scoreboard-abc123") == "scoreboard/scoreboard-abc123/config"
    assert config_topic("a") != config_topic("b")


def test_route_without_a_config_topic_still_works():
    """Older call sites pass four arguments; they must keep working."""
    got = {}
    Link.route("hockeytrack/games/today", b"{}", on_state=lambda *a: None,
               on_today=lambda b: got.setdefault("today", b))
    assert got["today"] == b"{}"


# --------------------------------------------------------------------------
# Who sent this: the owner, or the broker?
#
# The config topic is published RETAINED (cloud/cmd/api/handler.go), and this
# device resubscribes to it on every reconnect, so the broker hands the panel
# the same {"gameId": N} again every time the link comes back. The panel read
# each one as the owner choosing that game again -- re-arming the final hold
# and lighting the panel for five minutes, on a flaky link for ever.
#
# The flag that tells them apart is on the wire. MQTT 3.1.1 3.3.1.3: the
# server MUST set RETAIN=1 when a message is sent because of a NEW
# subscription [MQTT-3.3.1-8], and MUST set RETAIN=0 when it is sent because
# it matches an ESTABLISHED subscription, whatever the flag was on the
# message it received [MQTT-3.3.1-9]. So retain=1 means "replayed to me when
# I subscribed" and retain=0 means "somebody published this just now".
# --------------------------------------------------------------------------


def config_routed(payload=b'{"gameId":2026020001}', retain=False):
    mine = config_topic("scoreboard-abc123")
    got = {}
    sink = lambda *a: None
    Link.route(mine, payload, on_state=sink, on_today=sink,
               on_config=lambda b, r: got.update(payload=b, retain=r),
               config_topic_=mine, retain=retain)
    return got


def test_a_live_config_message_is_passed_on_as_live():
    assert config_routed(retain=False) == {"payload": b'{"gameId":2026020001}', "retain": False}


def test_a_replayed_config_message_is_passed_on_as_replayed():
    # The one the panel has to be able to tell apart: this is what arrives
    # every time the link comes back, with nobody doing anything.
    assert config_routed(retain=True)["retain"] is True


def test_the_flag_reaches_the_callback_off_the_wire():
    # paho sets MQTTMessage.retain from bit 0 of the PUBLISH header and does
    # nothing else with it (paho.mqtt.client._handle_publish), so what the
    # broker sent is what _on_message sees and what route passes on.
    import paho.mqtt.client as mqtt

    seen = {}
    link = Link.__new__(Link)          # no socket, no TLS: only the routing
    link.on_state = link.on_today = lambda *a: None
    link.on_config = lambda payload, retain: seen.update(payload=payload, retain=retain)
    link._config_topic = config_topic("scoreboard-abc123")

    message = mqtt.MQTTMessage(topic=link._config_topic.encode())
    message.payload = b'{"gameId":7}'
    message.retain = True
    link._on_message(None, None, message)

    assert seen == {"payload": b'{"gameId":7}', "retain": True}


# --------------------------------------------------------------------------
# A refused connection is not a connection
#
# paho calls on_connect for every CONNACK, including the ones that say no --
# a bad certificate, a policy that does not allow this client id, a broker
# refusing the client. The old callback subscribed and reported the link up
# regardless. With reconnect backoff capped at 60 s that cleared
# link_down_since every minute, so the "cannot reach the service" screen
# could never appear for the panel that needs it most: one that is reaching
# the broker and being turned away.
# --------------------------------------------------------------------------


class FakeClient:
    """What Link asks of paho's client: subscribe() returns (result, mid),
    and the mid is what a later SUBACK is matched to."""

    def __init__(self):
        self.subscribed = []
        self.qos = {}
        self.mids = {}

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)
        self.qos[topic] = qos
        self.mids[topic] = mid = len(self.subscribed)
        return 0, mid


def make_link(status_topics=()):
    link = Link.__new__(Link)
    link.on_state = link.on_today = link.on_config = lambda *a: None
    link._config_topic = config_topic("scoreboard-abc123")
    link._game = 2026020001
    link._status_topics = tuple(status_topics)
    link._status_pending, link._status_refused = {}, set()
    link.seen = []
    link.on_link = link.seen.append
    return link


def connect_with(reason_code, status_topics=()):
    link = make_link(status_topics)
    client = FakeClient()
    link._on_connect(client, None, {}, reason_code)
    return link.seen, client


class ReasonCode:
    """What paho hands the callback: something that knows whether it failed."""

    def __init__(self, failed, name):
        self.is_failure, self._name = failed, name

    def __str__(self):
        return self._name


def test_a_successful_connack_subscribes_and_reports_the_link_up():
    seen, client = connect_with(ReasonCode(False, "Success"))
    assert seen == [True]
    assert len(client.subscribed) == 3   # today, this device's config, the game


def test_a_refused_connack_reports_the_link_down_and_subscribes_to_nothing():
    seen, client = connect_with(ReasonCode(True, "Not authorized"))
    assert seen == [False], "a refusal was reported as a working link"
    assert client.subscribed == [], "subscribed on a connection that was refused"


def test_the_status_topics_are_subscribed_last_at_qos_0_and_on_every_connect():
    # design 8.1: the version rides to the site on a subscription nothing
    # publishes to. It comes after the topics that carry data, so a policy
    # that refused it could never cost the panel its state or config, and at
    # QoS 0 because nothing will ever arrive on it.
    topics = ["scoreboard/scoreboard-abc123/status/running/v0.1.6",
              "scoreboard/scoreboard-abc123/status/failed/v0.2.0"]
    seen, client = connect_with(ReasonCode(False, "Success"), topics)
    assert seen == [True]
    assert client.subscribed[-2:] == topics
    assert [client.qos[t] for t in topics] == [0, 0]
    seen, client = connect_with(ReasonCode(True, "Not authorized"), topics)
    assert client.subscribed == []


def test_a_link_built_without_status_topics_subscribes_to_none():
    seen, client = connect_with(ReasonCode(False, "Success"))
    assert not any("/status/" in t for t in client.subscribed)


STATUS_TOPICS = ["scoreboard/scoreboard-abc123/status/running/v0.1.6",
                 "scoreboard/scoreboard-abc123/status/failed/v0.2.0"]


def test_a_status_topic_the_broker_refuses_is_logged_once_and_not_asked_for_again(caplog):
    # A panel running this code against a policy without the status/*
    # filter (SCO-69 not yet applied). If the broker answers the SUBSCRIBE
    # with a SUBACK failure code, the topic is dropped for the life of the
    # process and the next connect asks only for the topics that carry
    # data plus the status topics that were granted.
    link = make_link(STATUS_TOPICS)
    client = FakeClient()
    link._on_connect(client, None, {}, ReasonCode(False, "Success"))
    assert link.seen == [True], "on_link fires before any SUBACK; the guard cannot change that"
    with caplog.at_level("WARNING", logger="scoreboard.link"):
        link._on_subscribe(client, None, client.mids[STATUS_TOPICS[0]], [ReasonCode(True, "Unspecified error")], None)
        link._on_subscribe(client, None, client.mids[STATUS_TOPICS[1]], [ReasonCode(False, "Granted QoS 0")], None)
    assert "refused the status subscription " + STATUS_TOPICS[0] in caplog.text
    assert "SCO-69" in caplog.text
    assert STATUS_TOPICS[1] not in caplog.text
    again = FakeClient()
    link._on_connect(again, None, {}, ReasonCode(False, "Success"))
    assert STATUS_TOPICS[0] not in again.subscribed
    assert again.subscribed[-1] == STATUS_TOPICS[1]
    assert len(again.subscribed) == 4, "today, config, the game, and the one granted status topic"


def test_the_data_topics_subacks_are_not_judged_by_the_status_guard():
    link = make_link(STATUS_TOPICS)
    client = FakeClient()
    link._on_connect(client, None, {}, ReasonCode(False, "Success"))
    for topic in ("hockeytrack/games/today", config_topic("scoreboard-abc123"), "hockeytrack/games/2026020001/state"):
        link._on_subscribe(client, None, client.mids[topic], [ReasonCode(True, "Not authorized")], None)
    assert link._status_refused == set()
    # An unknown mid, as after a reconnect cleared the pending map, is ignored.
    link._on_subscribe(client, None, 999, [ReasonCode(True, "Not authorized")], None)
    assert link._status_refused == set()


def test_an_mqtt3_granted_qos_byte_is_read_as_paho_would_hand_it():
    # paho builds ReasonCode from MQTT 3's granted-QoS byte, where 0x80 is
    # the one failure; a plain int is read by the same rule, and a shape
    # nothing can read is not a refusal.
    from scoreboard.link import _suback_failed
    assert [_suback_failed(c) for c in (0, 1, 2, 0x80)] == [False, False, False, True]
    assert not _suback_failed(Inscrutable())
    link = make_link(STATUS_TOPICS)
    client = FakeClient()
    link._on_connect(client, None, {}, ReasonCode(False, "Success"))
    link._on_subscribe(client, None, client.mids[STATUS_TOPICS[0]], [0x80], None)
    assert link._status_refused == {STATUS_TOPICS[0]}


def test_a_disconnect_forgets_the_subacks_still_awaited():
    link = make_link(STATUS_TOPICS)
    client = FakeClient()
    link._on_connect(client, None, {}, ReasonCode(False, "Success"))
    assert len(link._status_pending) == 2
    link._on_disconnect(client, None, {}, ReasonCode(False, "Success"))
    assert link._status_pending == {} and link.seen == [True, False]


def test_an_integer_reason_code_is_read_the_same_way():
    # paho's VERSION2 callbacks pass a ReasonCode object, but MQTT v3
    # brokers and older paho paths can hand over a plain int, where 0 is
    # success and anything else is a refusal.
    assert connect_with(0)[0] == [True]
    assert connect_with(5)[0] == [False]      # 5: not authorized
    assert connect_with(5)[1].subscribed == []


class ValueOnly:
    """A reason code that knows its number but not whether it is a failure.

    paho 2.1.0's ReasonCode has both `.value` and `.is_failure`; this is the
    shape left if a future paho drops the second, which N-6 is about.
    int(ReasonCode) raises TypeError -- it has no __int__ -- so the old
    fallback did not fall back at all.
    """

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return f"reason {self.value}"


class Inscrutable:
    """Something the panel cannot read at all: no is_failure, no value, no
    int()."""

    def __str__(self):
        return "who knows"


def test_a_reason_code_that_only_knows_its_number_is_read_by_that_number():
    # N-6: int(reason_code) raises TypeError for paho 2.1.0's ReasonCode, so
    # with is_failure gone the fallback raised rather than answering -- out
    # of a paho callback, where nothing catches it.
    assert connect_with(ValueOnly(0))[0] == [True]
    assert len(connect_with(ValueOnly(0))[1].subscribed) == 3
    assert connect_with(ValueOnly(5))[0] == [False]
    assert connect_with(ValueOnly(5))[1].subscribed == []


def test_a_reason_code_nothing_can_read_connects_anyway(caplog):
    # The decision, and it is the opposite of the one this code shipped
    # with. "Cannot tell" used to mean "refused", which sounds careful and
    # is in fact the only outcome here that bricks the panel: _refused()
    # returning True skips every SUBSCRIBE, so nothing can ever arrive, and
    # the panel shows "cannot reach the service" for ever on a link that is
    # working. Failing open costs nothing by comparison -- a CONNACK that
    # really was a refusal is followed by the broker closing the socket,
    # which fires _on_disconnect and reports the link down through the
    # ordinary path -- so the panel subscribes, says so, and lets the
    # disconnect tell the truth.
    with caplog.at_level("WARNING"):
        seen, client = connect_with(Inscrutable())
    assert seen == [True]
    assert len(client.subscribed) == 3
    assert any("who knows" in r.getMessage() for r in caplog.records), \
        "a reason code nothing could read went into the journal unremarked"
