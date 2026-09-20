#!/usr/bin/env bash
# Remove one panel's cloud identity: its certificate, its IoT thing, and its
# row in the devices table. For a panel that no longer exists -- a reflashed
# card, a dead board -- whose certificate is still ACTIVE with nobody holding
# the key. The site's "remove" only unbinds the owner; it does not revoke
# (SCO-24). Until that lands, this is the revocation path, run by the owner.
#
#   tools/retire-panel.sh <thing>                  dry run: prints what it found
#   tools/retire-panel.sh <thing> --apply <thing>  does it; the name is typed twice
#
# Everything here is irreversible: a retired panel has to be reflashed and
# claimed again. So: dry run by default, the name twice, a strict name pattern
# checked before any call is made, and a refusal to touch a certificate that
# is attached to any other thing.
#
# Order: the certificate goes INACTIVE first, which drops the panel from the
# broker at once; everything after is tidying, and any step failing stops the
# run. The devices row goes last so that, until the end, the site still shows
# the panel and a half-finished run is visible rather than silent. Re-running
# after an interruption picks up where it stopped.
set -euo pipefail
AWS=${AWS:-aws}
REGION=us-east-1
TABLE=scoreboard-devices

THING=${1:-}
MODE=${2:-}
CONFIRM=${3:-}

# scoreboard- and 2 to 32 of 0-9a-z: the names enrollment makes (twelve
# characters) and the first one, made by hand (scoreboard-01), which turned
# out to be an orphan too. Still nothing that is not a panel: the prefix, the
# alphabet and the length keep out every other thing in the account, and any
# shell metacharacter.
if ! [[ $THING =~ ^scoreboard-[0-9a-z]{2,32}$ ]]; then
  echo "usage: retire-panel.sh <thing> [--apply <thing>]   (thing: scoreboard- and 2 to 32 of 0-9a-z)" >&2
  exit 2
fi
if [ -n "$MODE" ] && { [ "$MODE" != "--apply" ] || [ "$CONFIRM" != "$THING" ]; }; then
  echo "refusing: to apply, give the name twice: retire-panel.sh $THING --apply $THING" >&2
  exit 2
fi

iot() { "$AWS" iot "$@" --region "$REGION"; }
ddb() { "$AWS" dynamodb "$@" --region "$REGION"; }
KEY="{\"thingName\":{\"S\":\"$THING\"}}"

thing=no
iot describe-thing --thing-name "$THING" --query thingName --output text >/dev/null 2>&1 && thing=yes

principals=()
if [ "$thing" = yes ]; then
  mapfile -t principals < <(iot list-thing-principals --thing-name "$THING" --query 'principals[]' --output text | tr '\t' '\n' | sed '/^$/d')
fi

# Only the key is projected: the row holds the owner's identity, and nothing
# here needs to read it.
row=$(ddb get-item --table-name "$TABLE" --key "$KEY" --projection-expression thingName --query 'Item.thingName.S' --output text | sed '/^None$/d')

for arn in "${principals[@]}"; do
  others=$(iot list-principal-things --principal "$arn" --query 'things[]' --output text | tr '\t' '\n' | sed "/^$/d;/^$THING\$/d")
  if [ -n "$others" ]; then
    echo "refusing: certificate ${arn##*/} is attached to another thing ($others). Not touching it." >&2
    exit 1
  fi
done

echo "panel:        $THING"
echo "thing:        $thing"
for arn in "${principals[@]}"; do id=${arn##*/}; echo "certificate:  ${id:0:12}…"; done
if [ -n "$row" ]; then echo "devices row:  yes"; else echo "devices row:  no"; fi

if [ "$thing" = no ] && [ -z "$row" ]; then
  echo "nothing to remove."
  exit 0
fi
if [ "$MODE" != "--apply" ]; then
  echo "dry run: nothing changed. To retire it: retire-panel.sh $THING --apply $THING"
  exit 0
fi

for arn in "${principals[@]}"; do
  id=${arn##*/}
  iot update-certificate --certificate-id "$id" --new-status INACTIVE
  echo "certificate ${id:0:12}… INACTIVE: the panel is off the broker"
  for policy in $(iot list-attached-policies --target "$arn" --query 'policies[].policyName' --output text); do
    iot detach-policy --policy-name "$policy" --target "$arn"
  done
  iot detach-thing-principal --thing-name "$THING" --principal "$arn"
  iot delete-certificate --certificate-id "$id"
  echo "certificate ${id:0:12}… deleted"
done
if [ "$thing" = yes ]; then
  iot delete-thing --thing-name "$THING"
  echo "thing deleted"
fi
if [ -n "$row" ]; then
  ddb delete-item --table-name "$TABLE" --key "$KEY"
  echo "devices row deleted"
fi
echo "retired: $THING"
