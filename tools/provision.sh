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

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/device/config"
TF_DIR="$ROOT/terraform"

if [ -e "$OUT" ]; then
  echo "refusing to run: $OUT already exists (a device is already provisioned here)." >&2
  echo "remove it yourself first if you really mean to re-provision." >&2
  exit 1
fi

tf_output() {
  terraform -chdir="$TF_DIR" output -raw "$1" 2>/dev/null
}

POLICY=$(tf_output device_policy_name || true)
if [ -z "$POLICY" ]; then
  echo "error: couldn't read 'device_policy_name' from terraform output in $TF_DIR." >&2
  echo "the cloud stack doesn't look deployed yet -- run 'make deploy' first." >&2
  exit 1
fi

mkdir -p "$OUT"
trap 'rmdir "$OUT" 2>/dev/null || true' EXIT

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
