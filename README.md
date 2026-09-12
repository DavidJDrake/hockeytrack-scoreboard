# HockeyTrack Scoreboard

A physical NHL scoreboard: a Raspberry Pi Zero 2 W driving a 4:1 HDMI bar
display that follows one live game — clock, score, shots, penalties and the
players serving them — pushed from
[HockeyTrack](https://github.com/DavidJDrake/hockeytrack)'s EventBridge bus
through AWS IoT Core within about a second of the play.

> **Status:** the cloud stack is deployed and the first device certificate is
> provisioned; the device software is written and tested. The hardware has
> not arrived yet, so there is no photo here and the end-to-end path has not
> been run against a real panel.

It exists as much to be a worked example as a gadget. HockeyTrack publishes
every game event to an EventBridge bus, and the point of this project is to
show what consuming that looks like from the outside: one rule on someone
else's bus, and nothing in the pipeline changes to accommodate you. See
[How it works](#how-it-works) below.

The full design — hardware options, the state document, the screen layout,
and the open questions — lives in
[`docs/superpowers/specs/2026-09-06-scoreboard-design.md`](docs/superpowers/specs/2026-09-06-scoreboard-design.md)
(also as a PDF in [`docs/pdf/`](docs/pdf/)), and the task-by-task
implementation history is in
[`docs/superpowers/plans/2026-09-06-scoreboard.md`](docs/superpowers/plans/2026-09-06-scoreboard.md).

## Architecture

```
HockeyTrack bus ──rule──▶ scoreboard reducer (Lambda) ──publish──▶ AWS IoT Core
   nhl.game.play                 keeps one state doc per        retained topics
   nhl.game.status               live game, republishes it      hockeytrack/games/<id>/state
   nhl.game.final                on every change                hockeytrack/games/today
   nhl.game.clock  (new)                                               │
   nhl.game.roster (new)                                               │ MQTT over TLS
                                                                       ▼
                                                              Pi Zero 2 W (Python)
                                                              subscribes to one game,
                                                              renders state, runs the
                                                              clock locally between updates
```

## Hardware

| Part | Choice | Why |
|---|---|---|
| Display | LESOWN 480×1920 bar monitor, HDMI (11.3" or 8.8") | 4:1 IPS, 60 Hz, powered over USB; any 4:1 HDMI panel works the same |
| Controller | Raspberry Pi Zero 2 W | $15, mini-HDMI, Wi-Fi, 512 MB RAM, runs Raspberry Pi OS Lite; renders 1920×480 with real fonts and logos |
| Cables | mini-HDMI → HDMI; two USB power leads | The Zero cannot power the monitor from its own USB port |
| Power | 5 V / 3 A supply (or two 5 V/2 A) | Zero 2 W ≈ 0.5 A, the monitor ≈ 1 A at full brightness |
| Buttons | Two momentary buttons on GPIO (optional) | Game select and brightness; the v2 web selector makes them optional |

Bar panels like these report themselves over HDMI as **480×1920 portrait**,
not landscape. Under KMS neither the firmware's `display_rotate` nor SDL will
turn the picture, so the device software does: it always draws a 1920×480
frame, then turns and scales it to whatever the display reports. The same
code letterboxes it on an ordinary TV, which is how the device can be
bench-tested before the panel exists.

## Setup

### Cloud

Deploys the reducer Lambda, the EventBridge rule, the DynamoDB table and the
IoT device policy into whichever AWS account already runs HockeyTrack's
event bus — this stack expects `hockeytrack` (or `var.bus_name`) to already
exist there.

```
make deploy
```

### Device

1. Provision an identity for the device (run once, from a machine with AWS
   credentials, **after** `make deploy` has applied):
   ```
   make provision DEVICE=living-room
   ```
   This creates an IoT thing and X.509 certificate and writes
   `device/config/device.json`, `device.pem.crt`, `private.pem.key` and
   `AmazonRootCA1.pem`. None of it is committed — treat the private key like
   a password.
2. Flash **Raspberry Pi OS Lite (64-bit), Trixie or later**, with Wi-Fi and
   a user of your choosing configured in Raspberry Pi Imager.

   Not Bookworm. The panel is driven through SDL's `kmsdrm` driver, and every
   pygame wheel on PyPI is built without it; only the distribution's
   `python3-pygame` has it. Bookworm's is 2.1.2, which fails
   `requirements.txt`, so pip would quietly replace it with a wheel and the
   panel would stay black. The service detects this and says so, but it is
   better not to get there.
3. On the Pi:
   ```
   sudo apt install -y git
   git clone https://github.com/DavidJDrake/hockeytrack-scoreboard.git
   ```
   Copy the `device/config/` directory from step 1 into
   `hockeytrack-scoreboard/device/config/`, then:
   ```
   hockeytrack-scoreboard/tools/pi-setup.sh
   ```
   Run it as the user the service should run as; it uses `sudo` itself. It
   refuses to start on Bookworm or with a missing or world-readable key,
   installs `python3-pygame` from apt, builds the venv with
   `--system-site-packages` and confirms pygame came from the system,
   adds you to the `video` and `render` groups (kmsdrm opens `/dev/dri`
   directly, with no X server to grant access), and installs, enables and
   starts `scoreboard.service` for your user and checkout path.
   `tools/pi-setup.sh --preflight` runs only the checks.

The first line the service logs (`journalctl -u scoreboard -f`) names the
video driver, the display size and the rotation it chose. If the picture is
upside down or turned the wrong way, add `"rotate"` to `device.json` — `90`,
`180` or `270` degrees clockwise, or `"auto"` (the default, which turns a
landscape frame a quarter turn on a portrait display) — and restart.

The display font, Barlow Condensed, is bundled in `device/scoreboard/fonts/`
under the SIL Open Font Licence.

### Choosing a game

Press button A (if fitted) to cycle through today's games; button B toggles
brightness. The device also follows a game id published, retained, to its own
`scoreboard/<thingName>/config` topic, which is how the planned
[admin site](docs/superpowers/specs/2026-09-07-admin-site-design.md) will
set it. Without either, follow a game directly by writing its id to the
state file:

```
echo '{"gameId": 2026020001}' > device/config/state.json
sudo systemctl restart scoreboard
```

### Admin API

The backend for the [admin site](docs/superpowers/specs/2026-09-07-admin-site-design.md)
mentioned above exists — a Lambda behind API Gateway, with a Cognito-signed-in
owner able to retarget a panel to a different game over MQTT — but the site
itself does not yet. Retargeting doesn't change what the device does: it
still only subscribes and renders, the buttons still work exactly as above,
and `device/config/state.json` is still the fallback when neither a button
nor a retained config message has set a game. See
[`docs/admin-api.md`](docs/admin-api.md) for the routes.

## How it works

The reducer folds bus events into one compact JSON document per game and
republishes it, retained, on every change. A device that reconnects — after
a reboot, a power cut or a Wi-Fi drop — is handed this document immediately,
with no history to replay:

```json
{
  "v": 1, "gameId": 2026020001, "state": "LIVE",
  "asOf": 1791135723123,
  "away": {"abbrev": "TBL", "score": 2, "sog": 17, "color": "002868"},
  "home": {"abbrev": "NYR", "score": 1, "sog": 22, "color": "0038A8"},
  "period": {"number": 2, "type": "REG", "label": "2"},
  "clock": {"seconds": 872, "running": true, "intermission": false},
  "situation": {"code": "1451", "pp": "TBL", "emptyNet": null},
  "penalties": [
    {"team": "NYR", "number": 23, "seconds": 74, "type": "MIN", "endsOnGoal": true}
  ],
  "lastGoal": {"team": "TBL", "number": 86, "asOf": 1791135690000},
  "start": "2026-10-01T23:30:00Z"
}
```

`asOf` is the reducer's publish time; the device counts the clock and
penalty `seconds` down locally between messages, so the display ticks every
second even though HockeyTrack samples the feed every five.

The entire integration with HockeyTrack is one EventBridge rule
(`terraform/rule.tf`):

```hcl
resource "aws_cloudwatch_event_rule" "game_events" {
  name           = "scoreboard-game-events"
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source        = ["hockeytrack.poller", "hockeytrack.synthetic"]
    "detail-type" = ["nhl.game.status", "nhl.game.clock", "nhl.game.play", "nhl.game.roster", "nhl.game.final"]
  })
}
```

Nothing in HockeyTrack's pipeline changes to accommodate this project. The
rule matches two event sources on purpose: `hockeytrack.poller` for real
games, and `hockeytrack.synthetic` for HockeyTrack's replay harness. The NHL
season only covers about half the year, so the harness — `make livefire
GAME=<id>` in the HockeyTrack repo — is how this device gets built and
bench-tested the rest of the time, by reconstructing any of HockeyTrack's
archived games and republishing it to the real bus at any speed, including
real time.

## Development

```
make test
```
runs the Go reducer's unit tests (`go vet` + `go test`) and the device's
Python tests (`pytest`, with `SDL_VIDEODRIVER=dummy` so it runs headless).

To see the renderer without a broker or any device config, point it at one
of the checked-in fixture state documents:

```
cd device
SCOREBOARD_FIXTURE=tests/fixtures/state_live.json .venv/bin/python -m scoreboard.main
```

This opens a 1920×480 window with the clock ticking from the fixture; Esc
quits, and the A/B keys stand in for the physical buttons. Add
`SCOREBOARD_WINDOW=240x960` to preview a portrait panel at quarter size, and
`SCOREBOARD_ROTATE=270` to try the other mounting.

`tools/bus_fixture.sh` drives the *real* deployed bus with the reducer's own
fixture events (game `2025020001`, CHI @ FLA, an id no real poller will ever
touch), a few seconds apart, so a bench device can be watched moving through
a full game end to end — PRE → penalty → goal flash → second period with a
power play → FINAL. It requires the cloud stack to be deployed first.

## Credits / Licence

MIT — see [`LICENSE`](LICENSE). Barlow Condensed is licensed separately
under the SIL Open Font Licence. NHL data is provided via
[HockeyTrack](https://github.com/DavidJDrake/hockeytrack); this project is
not affiliated with or endorsed by the NHL.
