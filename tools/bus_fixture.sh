#!/usr/bin/env bash
# Put the reducer's fixture events onto the real hockeytrack bus, a few
# seconds apart, so a bench device can be watched end to end. Uses game
# 2025020001 (CHI @ FLA), which no real poller will touch.
#
# NOT RUN as part of writing this repo: it mutates a real EventBridge bus
# and requires the cloud stack (reducer, rule, table, IoT policy) to already
# be deployed. Run it by hand on the bench once `make deploy` has applied.
set -euo pipefail
cd "$(dirname "$0")/../cloud/internal/reduce/testdata/events"
put() { # detail-type file [sleep]
  local detail
  detail=$(python3 -c 'import json,sys;print(json.dumps(open(sys.argv[1]).read()))' "$2")
  aws events put-events --region us-east-1 --entries "[{\"Source\":\"hockeytrack.poller\",\"EventBusName\":\"hockeytrack\",\"DetailType\":\"$1\",\"Detail\":$detail}]" --query 'FailedEntryCount' --output text
  sleep "${3:-3}"
}
put nhl.game.status status_live.json
put nhl.game.roster roster.json
put nhl.game.clock  clock_intermission.json
put nhl.game.play   penalty_chi_min.json
put nhl.game.play   goal_chi.json
put nhl.game.clock  clock_p2.json 1
put nhl.game.final  final.json
