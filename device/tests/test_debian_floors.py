"""device/requirements.txt against the Debian versions the image installs.

CI installs from PyPI and is satisfied by any floor; the image installs
Debian trixie's python3-* packages and pip never reaches PyPI there. A floor
above trixie's version (PR #44 raised cryptography to >=50.0.1 against
43.0.0) passed CI and would have failed a release build thirty-five minutes
in. device/debian-versions.txt pins what trixie ships, with the date it was
read; this test fails CI when a floor exceeds it, and tools/image-gate.sh
fails a build when the real rootfs disagrees with the file.

Not covered here: a Debian point release moving one of these versions. That
is what the gate catches, against the real rootfs; this test only knows what
the file says.
"""
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO / "device" / "requirements.txt"
PINS = REPO / "device" / "debian-versions.txt"


def requirements() -> dict[str, Requirement]:
    out = {}
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            req = Requirement(line)
            out[req.name.lower()] = req
    return out


def pins() -> dict[str, tuple[str, str, str]]:
    """pip name -> (Debian package, Debian version, date read)."""
    out = {}
    for line in PINS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        assert len(fields) == 4, f"debian-versions.txt: expected 4 fields, got {line!r}"
        name, package, version, read = fields
        out[name.lower()] = (package, version, read)
    return out


def upstream_version(debian_version: str) -> Version:
    """The part of a Debian version pip would compare against a floor.

    Strips the epoch (1:) and the Debian revision (-3+deb13u1), then keeps
    the leading dotted number so a +dfsg or ~rc suffix on the upstream part
    does not turn into a version pip cannot parse. The gate's shell does the
    same to both sides before comparing them.
    """
    without_epoch = re.sub(r"^\d+:", "", debian_version)
    upstream = without_epoch.rsplit("-", 1)[0] if "-" in without_epoch else without_epoch
    match = re.match(r"\d+(\.\d+)*", upstream)
    assert match, f"{debian_version!r} has no dotted version to compare"
    return Version(match.group(0))


def floor_of(req: Requirement) -> Version | None:
    floors = [Version(spec.version) for spec in req.specifier if spec.operator in (">=", "==", "~=")]
    return max(floors) if floors else None


def test_every_requirement_has_a_pinned_debian_version():
    # Every dependency comes from Debian on the image, so a requirement with
    # no pin is a package the image would have to fetch from PyPI, which
    # appliance mode forbids. And a pin nobody requires is a stale line.
    assert set(requirements()) == set(pins()), \
        f"requirements.txt names {sorted(requirements())} but debian-versions.txt pins {sorted(pins())}"


def test_the_pins_record_a_python3_package_and_the_date_they_were_read():
    for name, (package, version, read) in pins().items():
        assert package.startswith("python3-"), f"{name}: {package} is not a python3-* package"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", read), f"{name}: date read is {read!r}, not YYYY-MM-DD"
        upstream_version(version)  # parses, or the assertion inside says why not


@pytest.mark.parametrize("name", sorted(pins()))
def test_no_floor_exceeds_what_trixie_ships(name):
    req = requirements()[name]
    package, debian_version, read = pins()[name]
    floor = floor_of(req)
    assert floor is not None, f"{name}: requirements.txt sets no lower bound to check"
    shipped = upstream_version(debian_version)
    assert floor <= shipped, (
        f"{name}: requirements.txt asks for >={floor}, but Debian trixie's {package} is "
        f"{debian_version} (upstream {shipped}, read {read}). The image installs from "
        f"Debian and cannot follow PyPI; a floor above trixie's version fails the release "
        f"build, not CI. Lower the floor, or move the image off trixie first."
    )


@pytest.mark.parametrize("name", sorted(pins()))
def test_trixies_version_satisfies_the_whole_specifier(name):
    # The floor is the usual failure, but an upper bound (<3) has to hold
    # too, or appliance mode's pip --no-index rejects apt's package outright.
    req = requirements()[name]
    package, debian_version, _ = pins()[name]
    assert req.specifier.contains(upstream_version(debian_version)), \
        f"{name}: trixie's {package} {debian_version} does not satisfy {req.specifier}"


@pytest.mark.parametrize("debian, upstream", [
    ("43.0.0-3+deb13u1", "43.0.0"),
    ("2.1.0-1", "2.1.0"),
    ("2.6.1+dfsg-2+b1", "2.6.1"),
    ("1:2.6.1-1", "2.6.1"),
    ("3.0.0", "3.0.0"),
])
def test_the_upstream_version_is_read_the_way_pip_would_see_it(debian, upstream):
    assert upstream_version(debian) == Version(upstream)


def test_a_floor_above_trixie_is_the_failure_this_test_exists_for():
    # PR #44's shape: the floor Dependabot proposed against the version the
    # pin file holds. Kept as a fixture so the comparison itself is proven,
    # not only the happy path of the real files.
    req = Requirement("cryptography>=50.0.1")
    assert floor_of(req) > upstream_version("43.0.0-3+deb13u1")
