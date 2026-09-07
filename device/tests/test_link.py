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
               on_config=lambda b: got.setdefault("config", b), config_topic_=mine)
    assert got["config"] == b'{"gameId":2026020001}'
    assert "bad" not in got

    # Another device's config topic must not dispatch here.
    other = config_topic("scoreboard-someone-else")
    got2 = {}
    Link.route(other, b'{"gameId":1}', on_state=sink, on_today=sink,
               on_config=lambda b: got2.setdefault("config", b), config_topic_=mine)
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
