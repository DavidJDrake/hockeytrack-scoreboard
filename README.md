# HockeyTrack Scoreboard

A physical NHL scoreboard: a Raspberry Pi Zero 2 W driving a 4:1 HDMI bar
display that follows one live game — clock, score, shots, penalties and the
players serving them — pushed from
[HockeyTrack](https://github.com/DavidJDrake/hockeytrack)'s EventBridge bus
through AWS IoT Core within about a second of the play.

> **Status:** the cloud side and the device software are written and tested;
> the hardware has not been built yet, so there is no photo here and the
> end-to-end path has not been run against real hardware. The Terraform is
> validated but not applied.

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
   `AmazonRootCA1.pem`. None of it is committed — copy the whole
   `device/config/` directory to the Pi and treat the private key like a
   password.
2. Flash Raspberry Pi OS Lite (64-bit) to an SD card and boot it with Wi-Fi
   credentials configured.
3. On the Pi:
   ```
   sudo apt install python3-pygame libsdl2-2.0-0
   git clone https://github.com/DavidJDrake/hockeytrack-scoreboard.git
   cd hockeytrack-scoreboard/device
   python3 -m venv --system-site-packages .venv
   .venv/bin/pip install -r requirements.txt
   ```
   Copy the `device/config/` directory from step 1 onto the Pi at
   `hockeytrack-scoreboard/device/config/`, then:
   ```
   sudo usermod -aG video,render pi
   sudo cp device/scoreboard.service /etc/systemd/system/
   sudo systemctl enable --now scoreboard
   ```
   The `usermod` is a one-time step: `scoreboard.service` runs SDL's
   `kmsdrm` backend directly against `/dev/dri`, with no X server to grant
   access, so the service user needs to already be in the `video` and
   `render` groups. Log out (or reboot) after running it so the new group
   membership takes effect before the service starts.

Real fonts are optional: drop `BarlowCondensed-Bold.ttf` and
`BarlowCondensed-SemiBold.ttf` (available under the SIL Open Font Licence
from [Google Fonts](https://fonts.google.com/specimen/Barlow+Condensed))
into `device/scoreboard/fonts/`. Without them the renderer falls back to a
system condensed sans, or pygame's built-in default font, so nothing is
required for the display to work.

### Choosing a game

Press button A (if fitted) to cycle through today's games; button B toggles
brightness. Without buttons, follow a game directly by writing its id to the
state file:

```
echo '{"gameId": 2026020001}' > device/config/state.json
sudo systemctl restart scoreboard
```

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
quits, and the A/B keys stand in for the physical buttons.

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
