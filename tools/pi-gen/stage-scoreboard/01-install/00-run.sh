#!/bin/bash -e
# Install the appliance with the same script a hand-built panel uses, inside
# the image's chroot, then remove the source and record which build this is.
src="${ROOTFS_DIR}/tmp/scoreboard-src"
install -d "${src}/tools"
cp -a files/device "${src}/device"
install -m 755 files/pi-setup.sh "${src}/tools/pi-setup.sh"

on_chroot <<EOF
/tmp/scoreboard-src/tools/pi-setup.sh --appliance
EOF

rm -rf "${ROOTFS_DIR}/tmp/scoreboard-src"
install -m 644 files/scoreboard-build "${ROOTFS_DIR}/etc/scoreboard-build"
