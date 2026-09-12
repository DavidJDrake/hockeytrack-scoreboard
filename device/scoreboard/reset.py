"""Factory reset: return the panel to its just-flashed state."""
from __future__ import annotations

from pathlib import Path

IDENTITY_FILES = ("device.json", "device.pem.crt", "private.pem.key",
                  "AmazonRootCA1.pem", "state.json")


def factory_reset(config_dir: Path, nm) -> None:
    """Clear this device's identity and forget its saved networks.

    It deliberately does not revoke the certificate in the cloud. It cannot:
    the key it would authenticate with is the very thing being deleted. And
    a revocation any passer-by could trigger by holding two buttons would be
    a denial-of-service switch, not a security control. Revocation belongs
    to the owner, from the site (SCO-24).

    Deleting these files is best effort, not erasure: wear levelling on an
    SD card can leave the old blocks readable. That is why the confirmation
    screen tells the user to remove the device from their account rather
    than implying a cleared panel is safe to pass on.

    Only the files this project put there are removed, so a reset cannot
    take anything else on the device with it.
    """
    for name in IDENTITY_FILES:
        (config_dir / name).unlink(missing_ok=True)
    nm.forget_all()
