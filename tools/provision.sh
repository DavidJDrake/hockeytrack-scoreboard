#!/usr/bin/env bash
# Create an IoT thing + certificate for one scoreboard and write the files
# the device needs into device/config/. Run once per device.
#
# Reads the device policy name and IoT endpoint from `terraform output` in
# ../terraform, so it only works after `make deploy` (or `terraform apply`)
# has created that stack. Refuses to run if device/config/ already exists,
# so a second run cannot silently destroy a working device's identity.
set -euo pipefail
NAME=${1:?usage: provision.sh <thing-name>}
REGION=${REGION:-us-east-1}

# Terraform ships as a snap and refuses to run without a *writable*
# XDG_RUNTIME_DIR. A login shell usually points it at /run/user/$(id -u),
# which systemd-logind may never have created and which the user often cannot
# create either. The Makefile fixes this for its own recipes, but this script
# is meant to be runnable directly, so it has to fix it for itself.
if ! [ -w "${XDG_RUNTIME_DIR:-/nonexistent}" ]; then
  XDG_RUNTIME_DIR="$HOME/.cache/xdg-runtime"
  mkdir -p "$XDG_RUNTIME_DIR"
  chmod 700 "$XDG_RUNTIME_DIR"
  export XDG_RUNTIME_DIR
fi

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/device/config"
TF_DIR="$ROOT/terraform"

if [ -e "$OUT" ]; then
  echo "refusing to run: $OUT already exists (a device is already provisioned here)." >&2
  echo "remove it yourself first if you really mean to re-provision." >&2
  exit 1
fi

tf_err=$(mktemp)
tf_output() {
  terraform -chdir="$TF_DIR" output -raw "$1" 2>"$tf_err"
}

POLICY=$(tf_output device_policy_name || true)
if [ -z "$POLICY" ]; then
  echo "error: couldn't read 'device_policy_name' from terraform output in $TF_DIR." >&2
  if [ -s "$tf_err" ]; then
    # Terraform itself failed. Show why rather than guessing -- telling
    # someone "not deployed yet" when they just deployed it wastes an hour.
    echo "terraform said:" >&2
    sed 's/^/  /' "$tf_err" >&2
  else
    echo "the cloud stack doesn't look deployed yet -- run 'make deploy' first." >&2
  fi
  rm -f "$tf_err"
  exit 1
fi
rm -f "$tf_err"

mkdir -p "$OUT"

# CERT_ARN is set only once create-keys-and-certificate has succeeded, so
# cleanup() below knows whether there is a live AWS credential to unwind.
CERT_ARN=
cleanup() {
  # Runs on any non-zero exit (set -e + this EXIT trap) before the final
  # `trap - EXIT` disarms it on success. The exists-check above means this
  # invocation is always the one that created $OUT -- a pre-existing
  # device/config/ makes the script exit before mkdir, so the trap is never
  # even armed -- so it's safe to remove outright here.
  rm -rf "$OUT"
  if [ -n "$CERT_ARN" ]; then
    echo "partial run: deactivating and deleting certificate $CERT_ARN..." >&2
    local cert_id="${CERT_ARN##*/}"
    # Best-effort and in dependency order: a cert can't be deleted while
    # attached to a thing or (without --force-delete) a policy, and can't
    # be deleted while still ACTIVE. Ignore failures here -- we're already
    # on the failure path and want to get as much cleaned up as possible
    # rather than bail on the first error.
    aws iot detach-thing-principal --thing-name "$NAME" --principal "$CERT_ARN" --region "$REGION" 2>/dev/null || true
    aws iot detach-policy --policy-name "$POLICY" --target "$CERT_ARN" --region "$REGION" 2>/dev/null || true
    aws iot update-certificate --certificate-id "$cert_id" --new-status INACTIVE --region "$REGION" 2>/dev/null || true
    aws iot delete-certificate --certificate-id "$cert_id" --force-delete --region "$REGION" 2>/dev/null || true
  fi
}
trap cleanup EXIT

aws iot create-thing --thing-name "$NAME" --region "$REGION" >/dev/null
CERT_JSON=$(aws iot create-keys-and-certificate --set-as-active --region "$REGION" \
  --certificate-pem-outfile "$OUT/device.pem.crt" --private-key-outfile "$OUT/private.pem.key" --public-key-outfile "$OUT/public.pem.key")
CERT_ARN=$(echo "$CERT_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["certificateArn"])')
aws iot attach-policy --policy-name "$POLICY" --target "$CERT_ARN" --region "$REGION"
aws iot attach-thing-principal --thing-name "$NAME" --principal "$CERT_ARN" --region "$REGION"
curl -sS https://www.amazontrust.com/repository/AmazonRootCA1.pem -o "$OUT/AmazonRootCA1.pem"
ENDPOINT=$(tf_output iot_endpoint)
if [ -z "$ENDPOINT" ]; then
  ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$REGION" --query endpointAddress --output text)
fi
printf '{"thingName":"%s","endpoint":"%s","brightness":1.0}\n' "$NAME" "$ENDPOINT" > "$OUT/device.json"
chmod 600 "$OUT/private.pem.key"
trap - EXIT
echo "Provisioned $NAME. Copy device/config/ to the Pi at ~/hockeytrack-scoreboard/device/config/ (never commit it)."
