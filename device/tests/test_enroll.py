import json

import pytest

from scoreboard import enroll


class FakeAPI:
    """Stands in for the network. Records what was sent, replies from a script."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent: list[tuple[str, str, dict, dict]] = []

    def __call__(self, method, url, body, headers):
        self.sent.append((method, url, json.loads(body) if body else None, headers))
        if not self.replies:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


CREATED = (201, {"code": "7K4M9QX2", "display": "7K4M-9QX2", "token": "tok", "pollSeconds": 5})
WAITING = (202, {"status": "waiting to be claimed", "codeExpiresAt": 1757800000})
ROTATED = (202, {"status": "waiting to be claimed", "codeExpiresAt": 1757800900,
                 "code": "P9RT2WXY", "display": "P9RT-2WXY"})
CLAIMED = (200, {"certificatePem": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n",
                 "thingName": "scoreboard-abc123", "endpoint": "a1.iot.us-east-1.amazonaws.com"})


def test_the_first_step_submits_a_csr_and_shows_the_code(tmp_path):
    api = FakeAPI(CREATED)
    e = enroll.Enroller(tmp_path, owner="friend@example.com", transport=api)
    state = e.step()
    assert isinstance(state, enroll.Waiting)
    assert state.display == "7K4M-9QX2"
    assert state.owner == "friend@example.com"
    method, url, body, _ = api.sent[0]
    assert method == "POST"
    assert url.endswith("/api/enroll")
    assert "BEGIN CERTIFICATE REQUEST" in body["csr"]
    assert body["owner"] == "friend@example.com"


def test_a_panel_with_no_owner_line_sends_no_owner(tmp_path):
    api = FakeAPI(CREATED)
    enroll.Enroller(tmp_path, owner=None, transport=api).step()
    assert "owner" not in api.sent[0][2]


def test_polling_carries_the_token_as_a_bearer_and_keeps_the_code(tmp_path):
    api = FakeAPI(CREATED, WAITING)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()
    state = e.step()
    # A 202 with no code means "keep showing the one you have".
    assert isinstance(state, enroll.Waiting)
    assert state.display == "7K4M-9QX2"
    assert api.sent[1][3]["Authorization"] == "Bearer tok"
    assert api.sent[1][0] == "GET"


def test_a_rotated_code_replaces_the_one_on_screen(tmp_path):
    api = FakeAPI(CREATED, ROTATED)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()
    assert e.step().display == "P9RT-2WXY"


def test_a_claimed_enrollment_installs_the_identity(tmp_path):
    api = FakeAPI(CREATED, CLAIMED)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()
    state = e.step()
    assert isinstance(state, enroll.Ready)
    assert state.thing_name == "scoreboard-abc123"
    assert json.loads((tmp_path / "device.json").read_text())["thingName"] == "scoreboard-abc123"
    assert (tmp_path / "device.pem.crt").exists()
    # I-2: a token left behind after a successful claim would poll a dead row
    # on the next boot, 404, and re-enroll a panel that already has an
    # identity — putting a fresh pairing code on the screen of a device
    # somebody already owns.
    assert not (tmp_path / "enrollment.json").exists()


def test_a_failed_certificate_install_keeps_the_token_for_a_retry(tmp_path, monkeypatch):
    # I-2: installation happening before the token is forgotten must be
    # observable. If write_identity fails partway, the token has to survive
    # so the panel can poll again rather than losing a certificate somebody
    # already claimed.
    api = FakeAPI(CREATED, CLAIMED)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()

    def boom(*a, **k):
        raise OSError("card remounted read-only")

    monkeypatch.setattr(enroll.identity, "write_identity", boom)
    state = e.step()
    assert isinstance(state, enroll.Problem)
    assert (tmp_path / "enrollment.json").exists()
    assert not (tmp_path / "device.json").exists()


def test_a_claim_reply_missing_a_field_is_a_problem_not_a_crash(tmp_path):
    # C-1: the server has already deleted the row by the time these bytes
    # arrive, so a missing field must not crash the loop and lose the
    # certificate for good.
    incomplete_claim = (200, {"certificatePem": CLAIMED[1]["certificatePem"],
                              "thingName": "scoreboard-abc123"})  # no endpoint
    api = FakeAPI(CREATED, incomplete_claim)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step()
    state = e.step()
    assert isinstance(state, enroll.Problem)
    assert not (tmp_path / "device.json").exists()


def test_the_token_file_is_not_group_or_world_readable(tmp_path):
    # I-3: the token is a secret written to an SD card in a stranger's house;
    # the file mode is its only protection.
    enroll.Enroller(tmp_path, transport=FakeAPI(CREATED)).step()
    mode = (tmp_path / "enrollment.json").stat().st_mode
    assert mode & 0o077 == 0


def test_a_zero_poll_interval_is_clamped_to_the_fast_default(tmp_path):
    # I-4: pollSeconds comes off the one unauthenticated route in the
    # project; 0 must not turn the panel into a tight loop against it.
    zero_poll = (201, {"code": "AAAAAAAA", "display": "AAAA-AAAA", "token": "tok",
                       "pollSeconds": 0})
    e = enroll.Enroller(tmp_path, transport=FakeAPI(zero_poll))
    e.step()
    assert e.delay == enroll.BACKOFF[0]


def test_a_non_numeric_poll_interval_is_clamped_to_the_fast_default(tmp_path):
    junk_poll = (201, {"code": "BBBBBBBB", "display": "BBBB-BBBB", "token": "tok",
                       "pollSeconds": "soon"})
    e = enroll.Enroller(tmp_path, transport=FakeAPI(junk_poll))
    e.step()
    assert e.delay == enroll.BACKOFF[0]


def test_a_second_consecutive_404_backs_off_further_than_the_first(tmp_path):
    # I-5: a backend that keeps 404ing must not mint a fresh pending row
    # every few seconds forever. Repeated re-submission has to escalate
    # through BACKOFF like any other failure, not reset each time.
    enroll.Enroller(tmp_path, transport=FakeAPI(CREATED)).step()
    e = enroll.Enroller(tmp_path, transport=FakeAPI(
        (404, {"error": "no such enrollment"}), CREATED))
    e.step()
    first_delay = e.delay
    e.transport = FakeAPI((404, {"error": "no such enrollment"}), CREATED)
    e.step()
    assert e.delay > first_delay


def test_the_key_survives_a_reboot_mid_enrollment(tmp_path):
    api = FakeAPI(CREATED)
    first = enroll.Enroller(tmp_path, transport=api)
    first.step()
    key_before = (tmp_path / "private.pem.key").read_bytes()
    # New process, same card.
    enroll.Enroller(tmp_path, transport=FakeAPI(WAITING)).step()
    assert (tmp_path / "private.pem.key").read_bytes() == key_before


def test_the_token_survives_a_reboot_so_a_claimed_code_is_not_orphaned(tmp_path):
    # Losing the token means the row can never be collected: somebody claims
    # the code, a certificate is minted, and the panel that asked for it can
    # never pick it up. The server would have no way to know.
    enroll.Enroller(tmp_path, transport=FakeAPI(CREATED)).step()
    api = FakeAPI(WAITING)
    resumed = enroll.Enroller(tmp_path, transport=api)
    state = resumed.step()
    assert isinstance(state, enroll.Waiting)
    assert api.sent[0][0] == "GET", "a resumed panel must poll, not submit again"
    assert api.sent[0][3]["Authorization"] == "Bearer tok"


def test_a_404_starts_over_rather_than_polling_a_dead_row_forever(tmp_path):
    enroll.Enroller(tmp_path, transport=FakeAPI(CREATED)).step()
    api = FakeAPI((404, {"error": "no such enrollment"}), CREATED)
    resumed = enroll.Enroller(tmp_path, transport=api)
    state = resumed.step()
    assert isinstance(state, enroll.Waiting)
    assert [s[0] for s in api.sent] == ["GET", "POST"]


def test_a_network_failure_is_a_problem_the_screen_can_explain(tmp_path):
    api = FakeAPI(OSError("Name or service not known"))
    state = enroll.Enroller(tmp_path, transport=api).step()
    assert isinstance(state, enroll.Problem)
    assert state.detail


def test_a_server_error_is_a_problem_and_never_a_traceback(tmp_path):
    state = enroll.Enroller(tmp_path, transport=FakeAPI((500, {"error": "enrollment failed"}))).step()
    assert isinstance(state, enroll.Problem)


def test_the_backoff_settles_at_thirty_seconds(tmp_path):
    # The person on the other end is signing into a website and hunting for a
    # password. A panel that hammers the endpoint trips its own rate limit.
    api = FakeAPI(CREATED, WAITING, WAITING, WAITING, WAITING, WAITING, WAITING)
    e = enroll.Enroller(tmp_path, transport=api)
    delays = []
    for _ in range(7):
        e.step()
        delays.append(e.delay)
    assert delays[-1] == 30
    assert max(delays) == 30
    assert delays == sorted(delays), f"backoff must not go backwards: {delays}"


def test_a_problem_backs_off_too(tmp_path):
    e = enroll.Enroller(tmp_path, transport=FakeAPI(OSError("down"), OSError("down")))
    e.step()
    first = e.delay
    e.step()
    # M-5: a flat BACKOFF would satisfy >=; > is what "backs off too" means.
    assert e.delay > first


def test_no_secret_is_ever_logged(tmp_path, caplog):
    caplog.set_level("DEBUG")
    api = FakeAPI(CREATED, WAITING, CLAIMED)
    e = enroll.Enroller(tmp_path, transport=api)
    e.step(); e.step(); e.step()
    text = caplog.text
    assert "tok" not in text
    assert "7K4M9QX2" not in text and "7K4M-9QX2" not in text


def test_no_secret_is_ever_logged_across_every_branch(tmp_path, caplog):
    # I-6: CREATED -> WAITING -> CLAIMED only exercises the module's
    # quietest paths (one log line total). Walk the rotation, server-error,
    # network-error and 404 branches too, since those are where a future
    # `log.info("code rotated to %s", ...)`-style regression would land.
    caplog.set_level("DEBUG")
    api = FakeAPI(CREATED, ROTATED, (500, {"error": "enrollment failed"}),
                  OSError("down"), (404, {"error": "no such enrollment"}), CREATED, CLAIMED)
    e = enroll.Enroller(tmp_path, transport=api)
    for _ in range(6):
        e.step()
    text = caplog.text
    for secret in ("tok", "7K4M9QX2", "7K4M-9QX2", "P9RT2WXY", "P9RT-2WXY"):
        assert secret not in text


def test_the_first_screen_has_no_known_expiry_yet(tmp_path):
    # I-7: the 201 that creates the row carries no codeExpiresAt (only the
    # 202s that follow do), so the very first Waiting must read 0 rather
    # than something a caller could mistake for a real, already-past
    # timestamp.
    state = enroll.Enroller(tmp_path, transport=FakeAPI(CREATED)).step()
    assert state.expires_at == 0
