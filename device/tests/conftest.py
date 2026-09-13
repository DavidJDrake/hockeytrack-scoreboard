import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def no_enrollment_reaches_production():
    """Point every subprocess test at an endpoint that cannot answer.

    Any test that spawns ``scoreboard.main`` with no identity on disk starts
    a real ``enroll.Enroller``, and every subprocess test builds its ``env``
    from ``os.environ`` -- so setting this here, once, for the whole run
    means the guard is inherited rather than something each new test has to
    remember to add. Without it, ``enroll.API_BASE`` falls back to the
    baked-in production endpoint and the suite posts a CSR and a pairing
    code request to a live, anonymous-writable route on every run.

    An unroutable loopback port, not a bogus hostname: it fails fast with
    ECONNREFUSED instead of waiting out a DNS timeout.
    """
    os.environ["SCOREBOARD_API"] = "https://127.0.0.1:9"
