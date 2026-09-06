# HockeyTrack Scoreboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Raspberry Pi Zero 2 W driving a 480×1920 HDMI bar display that shows one chosen live NHL game (clock, period, score, shots, penalties, power play) from HockeyTrack's EventBridge bus, with a push path through AWS IoT Core so the panel updates within a second of the event.

**Architecture:** A Go Lambda ("reducer") subscribed to the `hockeytrack` bus folds `nhl.game.status/clock/play/roster/final` events into one compact state document per game, persisted in DynamoDB and published as a retained MQTT message on `hockeytrack/games/<gameId>/state`. A second small Lambda ("today") runs every 10 minutes, reads the public `data/schedule.json`, and publishes the day's game list on the retained topic `hockeytrack/games/today`. The device is a Python service (`paho-mqtt` + `pygame`) that subscribes to those topics and renders a 1920×480 frame at 10 Hz, counting the clock down locally between heartbeats.

**Tech Stack:** Go 1.27 (Lambda, `provided.al2023` on arm64 as a zip — no container, no Docker flatten step), Terraform ≥ 1.10, DynamoDB, AWS IoT Core (MQTT/TLS 8883, X.509 per device), Python 3.11 on Raspberry Pi OS Lite (Bookworm) with `paho-mqtt` 2.x and `pygame` 2.x, pytest.

**Spec:** `docs/superpowers/specs/2026-09-06-scoreboard-design.md` (this repo). The HockeyTrack-side events it depends on are planned in `/home/jay/projects/hockeytrack/docs/superpowers/plans/2026-09-06-scoreboard-events.md` and must be deployed before Task 14's end-to-end check; everything before that runs against fixtures.

## Global Constraints

- AWS account 989232581535, region `us-east-1`; the bus is named `hockeytrack`; alerts SNS topic is `hockeytrack-alerts`. Resource names are prefixed `scoreboard-`.
- State document version `"v": 1`; topics exactly `hockeytrack/games/<gameId>/state` and `hockeytrack/games/today`, both published with `retain: true`.
- Devices may only `iot:Connect` as their own thing name and `iot:Subscribe`/`iot:Receive` on `hockeytrack/games/*`. Devices publish nothing.
- The reducer's IAM role: `iot:Publish` on `arn:aws:iot:us-east-1:989232581535:topic/hockeytrack/games/*`, DynamoDB on its own table only, logs on its own group only. The rule target has a DLQ with a depth alarm to `hockeytrack-alerts`.
- Go: `gofmt`, `go vet ./...`, `go test ./...` clean; no AWS calls in unit tests. Go is at `~/.local/share/go/bin`.
- Python: `pytest` runs headless with `SDL_VIDEODRIVER=dummy`; no network in tests.
- Never commit `device/config/` (certificate, key, endpoint) or `secrets`; `.gitignore` covers them from Task 1.
- Terraform runs from this machine need `export XDG_RUNTIME_DIR=$HOME/.cache/xdg-runtime` (snap wrapper) — the Makefile sets it.
- EventBridge Scheduler roles must condition `aws:SourceArn` on the schedule **group** ARN (`schedule-group/<name>`), never `schedule/<group>/*` (lesson from HOC-27).

## File structure

```
hockeytrack-scoreboard/
  Makefile                      build (arm64 zips), test, deploy, provision
  .gitignore                    device/config/, build/, .terraform/, *.tfvars, __pycache__
  LICENSE                       MIT
  README.md
  cloud/
    go.mod                      module hockeytrack-scoreboard
    internal/reduce/
      state.go                  State document types + JSON; Situation parsing; period labels
      reduce.go                 Reduce(State, Event) (State, bool, error) — the pure fold
      penalties.go              penalty box: add, tick, end-on-goal
      teams.go                  32-entry abbrev → primary colour table
      reduce_test.go, penalties_test.go, state_test.go
      testdata/events/*.json    real-shaped HockeyTrack events (from the fixture game)
    internal/today/
      today.go                  Build(schedule, statesByGame, now) Doc
      today_test.go, testdata/schedule.json
    internal/iotpub/publisher.go   Publisher interface + IoT Data Plane impl + fake
    internal/gamestore/store.go    Store interface + DynamoDB impl + fake
    cmd/reducer/main.go         Lambda handler: store → reduce → store+publish
    cmd/today/main.go           Lambda handler: fetch schedule.json → publish today
  terraform/
    providers.tf, variables.tf, lambda.tf, rule.tf, dynamodb.tf, iot.tf, iam.tf, dlq.tf, scheduler.tf, outputs.tf
  device/
    scoreboard/__init__.py
    scoreboard/model.py         GameState from JSON; clock/penalty derivation at time t
    scoreboard/render.py        draw(surface, state, now, assets) at 1920×480
    scoreboard/assets.py        fonts/colours loading; logo lookup (optional files)
    scoreboard/link.py          MQTT client: connect, subscribe, callbacks, reconnect
    scoreboard/buttons.py       optional GPIO (gpiozero); no-op when unavailable
    scoreboard/main.py          service loop
    scoreboard/fonts/BarlowCondensed-{Bold,SemiBold}.ttf  (OFL)
    scoreboard.service          systemd unit
    requirements.txt            paho-mqtt>=2,<3; pygame>=2.5; gpiozero (Pi only)
    tests/test_model.py, tests/test_render.py, tests/fixtures/state_live.json …
  tools/provision.sh            creates thing+cert, writes device/config/
  docs/superpowers/specs, plans
```

---

### Task 1: Repository scaffold

**Files:**
- Create: `.gitignore`, `LICENSE`, `Makefile`, `README.md` (skeleton), `cloud/go.mod`, `cloud/internal/reduce/doc.go`, `device/requirements.txt`, `device/scoreboard/__init__.py`, `device/tests/__init__.py`

**Interfaces:**
- Produces: `make test` (Go + Python), `make build` (arm64 Lambda zips into `build/`), `make deploy`, `make provision DEVICE=<name>`.

- [ ] **Step 1: Write the ignore file and licence**

`.gitignore`:
```
build/
device/config/
device/.venv/
.venv/
__pycache__/
*.pyc
terraform/.terraform/
terraform/*.tfvars
terraform/*.tfplan
*.pem
*.key
.playwright-mcp/
```

`LICENSE`: MIT, copyright 2026 David J Drake (copy the text from `/home/jay/projects/hockeytrack/LICENSE` and keep the same name).

- [ ] **Step 2: Go module and a placeholder package so `go test ./...` has something to run**

`cloud/go.mod`:
```
module hockeytrack-scoreboard

go 1.27.0
```

`cloud/internal/reduce/doc.go`:
```go
// Package reduce folds HockeyTrack bus events into the scoreboard state
// document that devices render. It is a pure function of (state, event).
package reduce
```

Run: `cd cloud && export PATH="$HOME/.local/share/go/bin:$PATH" && go build ./... && go vet ./...`
Expected: no output.

- [ ] **Step 3: Python skeleton**

`device/requirements.txt`:
```
paho-mqtt>=2.1,<3
pygame>=2.5,<3
```
`device/requirements-dev.txt`:
```
-r requirements.txt
pytest>=8
```
Empty `device/scoreboard/__init__.py` and `device/tests/__init__.py`.

Run: `cd device && python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt && SDL_VIDEODRIVER=dummy .venv/bin/pytest -q`
Expected: `no tests ran` (exit code 5 is fine at this stage).

- [ ] **Step 4: Makefile**

```make
REGION      ?= us-east-1
export XDG_RUNTIME_DIR ?= $(HOME)/.cache/xdg-runtime
GO          := go
PY          := device/.venv/bin/python

.PHONY: test test-go test-py build deploy provision fmt

test: test-go test-py

test-go:
	cd cloud && $(GO) vet ./... && $(GO) test ./...

test-py:
	cd device && SDL_VIDEODRIVER=dummy .venv/bin/pytest -q

fmt:
	cd cloud && gofmt -l . && test -z "$$(gofmt -l .)"

# Lambda zips: static arm64 binaries named `bootstrap` for provided.al2023.
build:
	mkdir -p build/reducer build/today
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/reducer/bootstrap ./cmd/reducer
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/today/bootstrap ./cmd/today

deploy: test build
	mkdir -p $(XDG_RUNTIME_DIR)
	cd terraform && terraform apply -auto-approve

provision:
	@test -n "$(DEVICE)" || (echo "usage: make provision DEVICE=<thing-name>"; exit 2)
	./tools/provision.sh $(DEVICE)
```

- [ ] **Step 5: README skeleton** (filled in by Task 14)

```markdown
# HockeyTrack Scoreboard

A Raspberry Pi Zero 2 W and a 4:1 HDMI bar display showing one live NHL game,
fed by [HockeyTrack](https://github.com/DavidJDrake/hockeytrack)'s EventBridge
bus through AWS IoT Core. This repo is also the worked example of consuming
HockeyTrack from a separate project: one EventBridge rule, no changes to the
pipeline.

*Work in progress — see docs/superpowers/specs for the design.*
```

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "scaffold: Go module, Python package, Makefile, licence"
```

---

### Task 2: State document types

**Files:**
- Create: `cloud/internal/reduce/state.go`
- Test: `cloud/internal/reduce/state_test.go`

**Interfaces:**
- Produces:
```go
type Team struct { Abbrev string `json:"abbrev"`; Score int `json:"score"`; SOG int `json:"sog"`; Color string `json:"color"` }
type Period struct { Number int `json:"number"`; Type string `json:"type"`; Label string `json:"label"` }
type Clock struct { Seconds int `json:"seconds"`; Running bool `json:"running"`; Intermission bool `json:"intermission"` }
type Situation struct { Code string `json:"code"`; PP string `json:"pp,omitempty"`; EmptyNet string `json:"emptyNet,omitempty"` }
type Penalty struct {
	Team string `json:"team"`; Number int `json:"number"`; Seconds int `json:"seconds"`; Type string `json:"type"`; EndsOnGoal bool `json:"endsOnGoal"`
	StartT int `json:"-"`; Duration int `json:"-"`   // game-time seconds; internal
}
type Goal struct { Team string `json:"team"`; Number int `json:"number"`; AsOf int64 `json:"asOf"` }
type State struct {
	V int `json:"v"`; GameID int64 `json:"gameId"`; GameState string `json:"state"`   // PRE | LIVE | FINAL
	AsOf int64 `json:"asOf"`                                                          // ms since epoch
	Away, Home Team `json:"away" / "home"`; Period Period; Clock Clock; Situation Situation
	Penalties []Penalty `json:"penalties"`; LastGoal *Goal `json:"lastGoal,omitempty"`; Start string `json:"start,omitempty"`
	// internal bookkeeping (json "-"): LastSeq int64; Roster map[int64]int (playerId → number);
	// OTLen int (overtime period length in seconds); ObservedAt int64 (last heartbeat ms)
}
func (s State) JSON() ([]byte, error)           // marshals the public document
func ParseSituation(code, away, home string) Situation
func PeriodLabel(number int, periodType string) string
func OTLength(gameID int64) int                     // 1200 for playoff game ids (type 03), else 300
func PeriodLength(period, otLen int) int             // 1200 for periods 1–3, otLen after
func GameTime(period, elapsedInPeriod, otLen int) int // absolute game seconds
```
- Internal fields are excluded from JSON with `json:"-"`; persistence (Task 8) stores the whole struct via DynamoDB attributevalue tags, so give every internal field a `dynamodbav` tag too.

- [ ] **Step 1: Write the failing tests**

`cloud/internal/reduce/state_test.go`:
```go
package reduce

import (
	"encoding/json"
	"testing"
)

func TestParseSituation(t *testing.T) {
	cases := []struct{ code, pp, en string }{
		{"1551", "", ""},
		{"1451", "NYR", ""}, // away 4 skaters, home 5: home (NYR) on the PP
		{"1541", "TBL", ""},
		{"1441", "", ""},    // 4 on 4
		{"0651", "", "TBL"}, // away goalie pulled: 6 skaters, empty net TBL
		{"1560", "", "NYR"},
		{"", "", ""},
		{"abcd", "", ""},
	}
	for _, c := range cases {
		s := ParseSituation(c.code, "TBL", "NYR")
		if s.PP != c.pp || s.EmptyNet != c.en || s.Code != c.code {
			t.Errorf("%q: got pp=%q en=%q, want pp=%q en=%q", c.code, s.PP, s.EmptyNet, c.pp, c.en)
		}
	}
}

func TestPeriodLabel(t *testing.T) {
	for _, c := range []struct {
		n    int
		typ  string
		want string
	}{{1, "REG", "1"}, {3, "REG", "3"}, {4, "OT", "OT"}, {5, "OT", "2OT"}, {6, "OT", "3OT"}, {5, "SO", "SO"}, {0, "", ""}} {
		if got := PeriodLabel(c.n, c.typ); got != c.want {
			t.Errorf("PeriodLabel(%d,%q) = %q, want %q", c.n, c.typ, got, c.want)
		}
	}
}

func TestGameTime(t *testing.T) {
	if got := GameTime(1, 0, 300); got != 0 {
		t.Errorf("start = %d", got)
	}
	if got := GameTime(2, 418, 300); got != 1618 {
		t.Errorf("2nd period 6:58 = %d, want 1618", got)
	}
	if got := GameTime(4, 61, 300); got != 3661 {
		t.Errorf("regular-season OT 1:01 = %d, want 3661", got)
	}
	if got := GameTime(5, 0, 1200); got != 4800 {
		t.Errorf("playoff 2OT start = %d, want 4800", got)
	}
	if OTLength(2025020001) != 300 || OTLength(2025030112) != 1200 || OTLength(2026010001) != 300 {
		t.Error("OTLength must read the game-type digits of the id (02 regular, 03 playoffs)")
	}
	if PeriodLength(3, 1200) != 1200 || PeriodLength(4, 300) != 300 {
		t.Error("PeriodLength wrong")
	}
}

func TestStateJSONHidesInternals(t *testing.T) {
	s := State{V: 1, GameID: 1, GameState: "LIVE", LastSeq: 99, Roster: map[int64]int{8478010: 86}, OTLen: 300, Penalties: []Penalty{}}
	b, err := s.JSON()
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	json.Unmarshal(b, &m)
	for _, k := range []string{"LastSeq", "lastSeq", "Roster", "roster", "OTLen", "otLen", "ObservedAt", "Version"} {
		if _, ok := m[k]; ok {
			t.Errorf("internal field %q leaked into the document", k)
		}
	}
	if m["penalties"] == nil {
		t.Error("penalties must serialise as [] not null")
	}
	if _, ok := m["lastGoal"]; ok {
		t.Error("nil lastGoal must be omitted")
	}
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd cloud && go test ./internal/reduce/ -v`
Expected: FAIL to compile (types undefined).

- [ ] **Step 3: Implement `state.go`**

```go
package reduce

import (
	"encoding/json"
	"fmt"
)

type Team struct {
	Abbrev string `json:"abbrev" dynamodbav:"abbrev"`
	Score  int    `json:"score" dynamodbav:"score"`
	SOG    int    `json:"sog" dynamodbav:"sog"`
	Color  string `json:"color" dynamodbav:"color"`
}

type Period struct {
	Number int    `json:"number" dynamodbav:"number"`
	Type   string `json:"type" dynamodbav:"type"`
	Label  string `json:"label" dynamodbav:"label"`
}

type Clock struct {
	Seconds      int  `json:"seconds" dynamodbav:"seconds"`
	Running      bool `json:"running" dynamodbav:"running"`
	Intermission bool `json:"intermission" dynamodbav:"intermission"`
}

type Situation struct {
	Code     string `json:"code" dynamodbav:"code"`
	PP       string `json:"pp,omitempty" dynamodbav:"pp,omitempty"`
	EmptyNet string `json:"emptyNet,omitempty" dynamodbav:"emptyNet,omitempty"`
}

type Penalty struct {
	Team       string `json:"team" dynamodbav:"team"`
	Number     int    `json:"number" dynamodbav:"number"`
	Seconds    int    `json:"seconds" dynamodbav:"seconds"`
	Type       string `json:"type" dynamodbav:"type"` // MIN, MAJ, BEN, MIS, MAT
	EndsOnGoal bool   `json:"endsOnGoal" dynamodbav:"endsOnGoal"`
	StartT     int    `json:"-" dynamodbav:"startT"`   // absolute game seconds when assessed
	Duration   int    `json:"-" dynamodbav:"duration"` // seconds
}

type Goal struct {
	Team   string `json:"team" dynamodbav:"team"`
	Number int    `json:"number" dynamodbav:"number"`
	AsOf   int64  `json:"asOf" dynamodbav:"asOf"`
}

// State is the document devices render. Fields tagged json:"-" are
// bookkeeping the reducer needs between events; they persist in DynamoDB
// but never reach the topic.
type State struct {
	V         int       `json:"v" dynamodbav:"v"`
	GameID    int64     `json:"gameId" dynamodbav:"gameId"`
	GameState string    `json:"state" dynamodbav:"state"` // PRE | LIVE | FINAL
	AsOf      int64     `json:"asOf" dynamodbav:"asOf"`
	Away      Team      `json:"away" dynamodbav:"away"`
	Home      Team      `json:"home" dynamodbav:"home"`
	Period    Period    `json:"period" dynamodbav:"period"`
	Clock     Clock     `json:"clock" dynamodbav:"clock"`
	Situation Situation `json:"situation" dynamodbav:"situation"`
	Penalties []Penalty `json:"penalties" dynamodbav:"penalties"`
	LastGoal  *Goal     `json:"lastGoal,omitempty" dynamodbav:"lastGoal,omitempty"`
	Start     string    `json:"start,omitempty" dynamodbav:"start,omitempty"`

	LastSeq    int64         `json:"-" dynamodbav:"lastSeq"`
	Roster     map[int64]int `json:"-" dynamodbav:"roster"`
	OTLen      int           `json:"-" dynamodbav:"otLen"`
	ObservedAt int64         `json:"-" dynamodbav:"observedAt"`
	Version    int64         `json:"-" dynamodbav:"version"` // optimistic lock
}

// JSON marshals the public document; nil slices become [] so devices can
// index them without null checks.
func (s State) JSON() ([]byte, error) {
	if s.Penalties == nil {
		s.Penalties = []Penalty{}
	}
	return json.Marshal(s)
}

// ParseSituation decodes the NHL's four-digit situation code
// (away goalie, away skaters, home skaters, home goalie).
func ParseSituation(code, away, home string) Situation {
	s := Situation{Code: code}
	if len(code) != 4 {
		return s
	}
	var d [4]int
	for i := 0; i < 4; i++ {
		if code[i] < '0' || code[i] > '9' {
			return s
		}
		d[i] = int(code[i] - '0')
	}
	awayGoalie, awaySk, homeSk, homeGoalie := d[0], d[1], d[2], d[3]
	switch {
	case homeSk > awaySk:
		s.PP = home
	case awaySk > homeSk:
		s.PP = away
	}
	switch {
	case awayGoalie == 0 && awaySk > 0:
		s.EmptyNet = away
	case homeGoalie == 0 && homeSk > 0:
		s.EmptyNet = home
	}
	return s
}

// PeriodLabel renders 1/2/3, OT, 2OT…, SO.
func PeriodLabel(number int, periodType string) string {
	switch {
	case number == 0:
		return ""
	case periodType == "SO":
		return "SO"
	case periodType == "OT" && number <= 4:
		return "OT"
	case periodType == "OT":
		return fmt.Sprintf("%dOT", number-3)
	default:
		return fmt.Sprintf("%d", number)
	}
}

// OTLength is the overtime period length implied by the game id: the two
// digits after the season are the game type (01 preseason, 02 regular
// season, 03 playoffs); only playoff overtime is a full 20 minutes.
func OTLength(gameID int64) int {
	if (gameID/10000)%100 == 3 {
		return 1200
	}
	return 300
}

// PeriodLength is 20 minutes for regulation and otLen after that.
func PeriodLength(period, otLen int) int {
	if period <= 3 {
		return 1200
	}
	if otLen == 0 {
		return 300
	}
	return otLen
}

// GameTime converts (period, seconds elapsed in period) to absolute game
// seconds.
func GameTime(period, elapsedInPeriod, otLen int) int {
	t := 0
	for p := 1; p < period; p++ {
		t += PeriodLength(p, otLen)
	}
	return t + elapsedInPeriod
}
```

- [ ] **Step 4: Run the tests**

Run: `cd cloud && go test ./internal/reduce/ -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add cloud/internal/reduce && git commit -m "reduce: state document types, situation code, period labels, game time"
```

---

### Task 3: Event fixtures and the status/clock folds

**Files:**
- Create: `cloud/internal/reduce/reduce.go`, `cloud/internal/reduce/testdata/events/status_live.json`, `clock_p2.json`, `clock_intermission.json`, `roster.json`, `goal_chi.json`, `penalty_chi_min.json`, `final.json`
- Test: `cloud/internal/reduce/reduce_test.go`

**Interfaces:**
- Produces:
```go
type Event struct { DetailType string; Detail json.RawMessage; Time time.Time }   // Time = EventBridge `time`
func Reduce(s State, e Event) (State, bool, error)   // bool: document changed → publish
```
- Detail-type strings and payload shapes are HockeyTrack's (`nhl.game.status/clock/play/roster/final`); the reducer defines its own minimal structs for decoding them (never imports HockeyTrack).

- [ ] **Step 1: Write the fixtures** (real shapes; ids/teams from HockeyTrack's fixture game CHI @ FLA, 2025020001)

`testdata/events/status_live.json`:
```json
{"schemaVersion":1,"gameId":2025020001,"prevState":"PRE","gameState":"LIVE","score":{"CHI":0,"FLA":0}}
```
`testdata/events/clock_p2.json`:
```json
{"schemaVersion":1,"gameId":2025020001,"gameState":"LIVE","period":2,"periodType":"REG","secondsRemaining":872,"timeRemaining":"14:32","running":true,"inIntermission":false,"situationCode":"1451","homeTeam":"FLA","awayTeam":"CHI","score":{"CHI":2,"FLA":1},"shots":{"CHI":17,"FLA":22},"observedAt":"2025-10-07T22:31:05Z"}
```
`testdata/events/clock_intermission.json`:
```json
{"schemaVersion":1,"gameId":2025020001,"gameState":"LIVE","period":1,"periodType":"REG","secondsRemaining":0,"timeRemaining":"00:00","running":false,"inIntermission":true,"situationCode":"1551","homeTeam":"FLA","awayTeam":"CHI","score":{"CHI":1,"FLA":0},"shots":{"CHI":9,"FLA":13},"observedAt":"2025-10-07T21:52:10Z"}
```
`testdata/events/roster.json`:
```json
{"schemaVersion":1,"gameId":2025020001,"homeTeam":"FLA","awayTeam":"CHI","players":[
 {"playerId":8483493,"team":"CHI","number":91,"position":"C"},
 {"playerId":8484783,"team":"CHI","number":81,"position":"R"},
 {"playerId":8473419,"team":"FLA","number":63,"position":"L"},
 {"playerId":8475683,"team":"FLA","number":72,"position":"G"}]}
```
`testdata/events/goal_chi.json` (the `raw` block is the real play from the HockeyTrack fixture, trimmed of clip URLs):
```json
{"schemaVersion":1,"gameId":2025020001,"seq":166,"playType":"goal","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","scoringTeam":"CHI","period":1,"timeInPeriod":"10:03","score":{"CHI":1,"FLA":0},
 "raw":{"eventId":258,"periodDescriptor":{"number":1,"periodType":"REG","maxRegulationPeriods":3},"timeInPeriod":"10:03","timeRemaining":"09:57","situationCode":"1551","typeCode":505,"typeDescKey":"goal","sortOrder":166,"details":{"xCoord":66,"yCoord":-1,"zoneCode":"O","shotType":"snap","scoringPlayerId":8483493,"scoringPlayerTotal":1,"assist1PlayerId":8477479,"assist2PlayerId":8476882,"eventOwnerTeamId":16,"goalieInNetId":8475683,"awayScore":1,"homeScore":0}}}
```
`testdata/events/penalty_chi_min.json`:
```json
{"schemaVersion":1,"gameId":2025020001,"seq":125,"playType":"penalty","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","period":1,"timeInPeriod":"06:58","score":{"CHI":0,"FLA":0},
 "raw":{"eventId":218,"periodDescriptor":{"number":1,"periodType":"REG","maxRegulationPeriods":3},"timeInPeriod":"06:58","timeRemaining":"13:02","situationCode":"1551","typeCode":509,"typeDescKey":"penalty","sortOrder":125,"details":{"xCoord":2,"yCoord":2,"zoneCode":"N","typeCode":"MIN","descKey":"slashing","duration":2,"committedByPlayerId":8484783,"drawnByPlayerId":8482113,"eventOwnerTeamId":16}}}
```
`testdata/events/final.json`:
```json
{"schemaVersion":1,"gameId":2025020001,"homeTeam":"FLA","awayTeam":"CHI","score":{"CHI":2,"FLA":3},"s3Prefix":"raw/20252026/2025-10-07/2025020001/"}
```

- [ ] **Step 2: Write the failing tests for status and clock**

`cloud/internal/reduce/reduce_test.go`:
```go
package reduce

import (
	"encoding/json"
	"os"
	"testing"
	"time"
)

func load(t *testing.T, detailType, file string) Event {
	t.Helper()
	b, err := os.ReadFile("testdata/events/" + file)
	if err != nil {
		t.Fatal(err)
	}
	return Event{DetailType: detailType, Detail: json.RawMessage(b), Time: time.Date(2025, 10, 7, 22, 31, 6, 0, time.UTC)}
}

func TestStatusEventStartsAGame(t *testing.T) {
	s, changed, err := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.V != 1 || s.GameID != 2025020001 || s.GameState != "LIVE" {
		t.Errorf("state = %+v", s)
	}
	if s.Away.Abbrev != "CHI" || s.Home.Abbrev != "FLA" {
		t.Errorf("teams from score map: away=%q home=%q", s.Away.Abbrev, s.Home.Abbrev)
	}
	if s.AsOf == 0 {
		t.Error("asOf must be set from the event time")
	}
}

func TestClockHeartbeatUpdatesEverything(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, changed, err := Reduce(s, load(t, "nhl.game.clock", "clock_p2.json"))
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.Period.Number != 2 || s.Period.Label != "2" || s.Clock.Seconds != 872 || !s.Clock.Running || s.Clock.Intermission {
		t.Errorf("period/clock = %+v / %+v", s.Period, s.Clock)
	}
	if s.Away.Score != 2 || s.Home.Score != 1 || s.Away.SOG != 17 || s.Home.SOG != 22 {
		t.Errorf("score/sog = %+v / %+v", s.Away, s.Home)
	}
	if s.Situation.PP != "FLA" || s.Situation.Code != "1451" {
		t.Errorf("situation = %+v", s.Situation)
	}
	if s.Away.Color == "" || s.Home.Color == "" {
		t.Error("team colours should be filled from the table")
	}
	if s.OTLen != 300 {
		t.Errorf("otLen = %d, want 300 for a regular-season id", s.OTLen)
	}
	if s.AsOf != time.Date(2025, 10, 7, 22, 31, 6, 0, time.UTC).UnixMilli() {
		t.Errorf("asOf = %d", s.AsOf)
	}
}

func TestStaleClockIsIgnored(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.clock", "clock_p2.json"))
	older := load(t, "nhl.game.clock", "clock_intermission.json") // observedAt 21:52 < 22:31
	s2, changed, err := Reduce(s, older)
	if err != nil {
		t.Fatal(err)
	}
	if changed || s2.Period.Number != 2 {
		t.Errorf("stale heartbeat applied: changed=%v period=%d", changed, s2.Period.Number)
	}
}

func TestIntermissionClock(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.clock", "clock_intermission.json"))
	if !s.Clock.Intermission || s.Clock.Running || s.Clock.Seconds != 0 || s.Period.Number != 1 {
		t.Errorf("intermission state = %+v / %+v", s.Clock, s.Period)
	}
}

func TestUnknownDetailTypeIsIgnored(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s2, changed, err := Reduce(s, Event{DetailType: "hockeytrack.alert", Detail: json.RawMessage(`{"gameId":2025020001,"reason":"x"}`)})
	if err != nil || changed || s2.GameState != "LIVE" {
		t.Errorf("err=%v changed=%v state=%+v", err, changed, s2)
	}
}
```

- [ ] **Step 3: Run to verify failure**

Run: `cd cloud && go test ./internal/reduce/ -run 'Status|Clock|Intermission|Unknown' -v`
Expected: FAIL to compile (`Event`, `Reduce` undefined).

- [ ] **Step 4: Implement `reduce.go` (status + clock; play/roster/final stubs return unchanged)**

```go
package reduce

import (
	"encoding/json"
	"fmt"
	"sort"
	"time"
)

// Event is one EventBridge event from the hockeytrack bus.
type Event struct {
	DetailType string
	Detail     json.RawMessage
	Time       time.Time
}

// Minimal decoders for HockeyTrack's detail-types. Only the fields the
// scoreboard needs; unknown fields are ignored.
type statusDetail struct {
	GameID    int64          `json:"gameId"`
	GameState string         `json:"gameState"`
	Score     map[string]int `json:"score"`
}

type clockDetail struct {
	GameID           int64          `json:"gameId"`
	GameState        string         `json:"gameState"`
	Period           int            `json:"period"`
	PeriodType       string         `json:"periodType"`
	SecondsRemaining int            `json:"secondsRemaining"`
	Running          bool           `json:"running"`
	InIntermission   bool           `json:"inIntermission"`
	SituationCode    string         `json:"situationCode"`
	HomeTeam         string         `json:"homeTeam"`
	AwayTeam         string         `json:"awayTeam"`
	Score            map[string]int `json:"score"`
	Shots            map[string]int `json:"shots"`
	ObservedAt       time.Time      `json:"observedAt"`
}

type playDetail struct {
	GameID       int64          `json:"gameId"`
	Seq          int64          `json:"seq"`
	PlayType     string         `json:"playType"`
	HomeTeam     string         `json:"homeTeam"`
	AwayTeam     string         `json:"awayTeam"`
	ActingTeam   string         `json:"actingTeam"`
	ScoringTeam  string         `json:"scoringTeam"`
	Period       int            `json:"period"`
	TimeInPeriod string         `json:"timeInPeriod"`
	Score        map[string]int `json:"score"`
	Raw          struct {
		PeriodDescriptor struct {
			Number     int    `json:"number"`
			PeriodType string `json:"periodType"`
		} `json:"periodDescriptor"`
		Details struct {
			TypeCode            string `json:"typeCode"`
			Duration            int    `json:"duration"`
			CommittedByPlayerID int64  `json:"committedByPlayerId"`
			ServedByPlayerID    int64  `json:"servedByPlayerId"`
			ScoringPlayerID     int64  `json:"scoringPlayerId"`
		} `json:"details"`
	} `json:"raw"`
}

type rosterDetail struct {
	GameID   int64  `json:"gameId"`
	HomeTeam string `json:"homeTeam"`
	AwayTeam string `json:"awayTeam"`
	Players  []struct {
		PlayerID int64  `json:"playerId"`
		Team     string `json:"team"`
		Number   int    `json:"number"`
	} `json:"players"`
}

type finalDetail struct {
	GameID   int64          `json:"gameId"`
	HomeTeam string         `json:"homeTeam"`
	AwayTeam string         `json:"awayTeam"`
	Score    map[string]int `json:"score"`
}

// Reduce folds one event into the state. The bool reports whether the
// public document changed and should be republished.
func Reduce(s State, e Event) (State, bool, error) {
	s.V = 1
	if s.Roster == nil {
		s.Roster = map[int64]int{}
	}
	if s.Penalties == nil {
		s.Penalties = []Penalty{}
	}
	stamp := func() { s.AsOf = e.Time.UTC().UnixMilli() }

	switch e.DetailType {
	case "nhl.game.status":
		var d statusDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("status: %w", err)
		}
		s.GameID = d.GameID
		s.GameState = mapGameState(d.GameState)
		// The status event carries no home/away; the score map's key order
		// is not stable, so only fill abbreviations if we have none yet and
		// let the clock/play events (which do carry them) correct it.
		s.applyScore(d.Score)
		if s.GameState == "PRE" {
			s.Penalties = []Penalty{}
		}
		stamp()
		return s, true, nil

	case "nhl.game.clock":
		var d clockDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("clock: %w", err)
		}
		if obs := d.ObservedAt.UnixMilli(); obs < s.ObservedAt {
			return s, false, nil // out-of-order heartbeat
		} else {
			s.ObservedAt = obs
		}
		s.GameID = d.GameID
		s.GameState = mapGameState(d.GameState)
		s.setTeams(d.AwayTeam, d.HomeTeam)
		s.applyScore(d.Score)
		s.Away.SOG, s.Home.SOG = d.Shots[d.AwayTeam], d.Shots[d.HomeTeam]
		s.OTLen = OTLength(d.GameID)
		if d.Period > 0 {
			s.Period = Period{Number: d.Period, Type: d.PeriodType, Label: PeriodLabel(d.Period, d.PeriodType)}
		}
		s.Clock = Clock{Seconds: d.SecondsRemaining, Running: d.Running, Intermission: d.InIntermission}
		s.Situation = ParseSituation(d.SituationCode, d.AwayTeam, d.HomeTeam)
		s.tickPenalties()
		stamp()
		return s, true, nil

	case "nhl.game.play":
		return s.applyPlay(e)

	case "nhl.game.roster":
		var d rosterDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("roster: %w", err)
		}
		s.GameID = d.GameID
		s.setTeams(d.AwayTeam, d.HomeTeam)
		for _, p := range d.Players {
			s.Roster[p.PlayerID] = p.Number
		}
		return s, false, nil // roster alone changes nothing visible

	case "nhl.game.final":
		var d finalDetail
		if err := json.Unmarshal(e.Detail, &d); err != nil {
			return s, false, fmt.Errorf("final: %w", err)
		}
		s.GameID = d.GameID
		s.setTeams(d.AwayTeam, d.HomeTeam)
		s.applyScore(d.Score)
		s.GameState = "FINAL"
		s.Clock.Running = false
		s.Penalties = []Penalty{}
		s.Situation = Situation{Code: s.Situation.Code}
		stamp()
		return s, true, nil
	}
	return s, false, nil
}

// mapGameState collapses the NHL's states into the three the panel shows.
func mapGameState(nhl string) string {
	switch nhl {
	case "LIVE", "CRIT":
		return "LIVE"
	case "FINAL", "OFF":
		return "FINAL"
	default:
		return "PRE"
	}
}

func (s *State) setTeams(away, home string) {
	if away != "" {
		s.Away.Abbrev = away
		s.Away.Color = TeamColor(away)
	}
	if home != "" {
		s.Home.Abbrev = home
		s.Home.Color = TeamColor(home)
	}
}

func (s *State) applyScore(score map[string]int) {
	if s.Away.Abbrev == "" && s.Home.Abbrev == "" && len(score) == 2 {
		// First sight of the game via a status event: we know the two
		// abbreviations but not which is home. Use alphabetical order for
		// now; the first clock or play event (which carry home/away) fixes it.
		keys := make([]string, 0, 2)
		for k := range score {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		s.setTeams(keys[0], keys[1])
	}
	if v, ok := score[s.Away.Abbrev]; ok {
		s.Away.Score = v
	}
	if v, ok := score[s.Home.Abbrev]; ok {
		s.Home.Score = v
	}
}
```

`applyPlay`, `tickPenalties` and `TeamColor` are defined in Tasks 4–6; to compile now, add temporary stubs at the bottom of `reduce.go` and delete them when those tasks land:

```go
func (s State) applyPlay(e Event) (State, bool, error) { return s, false, nil }
func (s *State) tickPenalties()                         {}
func TeamColor(abbrev string) string                    { return "888888" }
```

- [ ] **Step 5: Run the tests**

Run: `cd cloud && go test ./internal/reduce/ -v`
Expected: PASS for the five new tests plus Task 2's.

- [ ] **Step 6: Commit**

```bash
git add cloud/internal/reduce && git commit -m "reduce: status and clock folds with real-shaped event fixtures"
```

---

### Task 4: Team colour table

**Files:**
- Create: `cloud/internal/reduce/teams.go`
- Test: append to `cloud/internal/reduce/state_test.go`

**Interfaces:**
- Produces: `func TeamColor(abbrev string) string` — 6-hex primary colour, `"888888"` for unknown.

- [ ] **Step 1: Write the failing test**

```go
func TestTeamColor(t *testing.T) {
	if TeamColor("TBL") != "002868" || TeamColor("NYR") != "0038A8" || TeamColor("FLA") != "C8102E" {
		t.Errorf("known colours wrong: %s %s %s", TeamColor("TBL"), TeamColor("NYR"), TeamColor("FLA"))
	}
	if TeamColor("XXX") != "888888" {
		t.Errorf("unknown = %s", TeamColor("XXX"))
	}
	if len(teamColors) != 32 {
		t.Errorf("table has %d teams, want 32", len(teamColors))
	}
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd cloud && go test ./internal/reduce/ -run TestTeamColor -v`
Expected: FAIL (`teamColors` undefined; stub returns 888888 for all).

- [ ] **Step 3: Implement `teams.go`** and delete the `TeamColor` stub from `reduce.go`

```go
package reduce

// Primary colours per club (hex, no #). Source: each club's published
// brand guide; approximations where the guide is not public.
var teamColors = map[string]string{
	"ANA": "F47A38", "BOS": "FFB81C", "BUF": "003087", "CGY": "D2001C",
	"CAR": "CC0000", "CHI": "CF0A2C", "COL": "6F263D", "CBJ": "002654",
	"DAL": "006847", "DET": "CE1126", "EDM": "FF4C00", "FLA": "C8102E",
	"LAK": "111111", "MIN": "154734", "MTL": "AF1E2D", "NSH": "FFB81C",
	"NJD": "CE1126", "NYI": "00539B", "NYR": "0038A8", "OTT": "C52032",
	"PHI": "F74902", "PIT": "FCB514", "SJS": "006D75", "SEA": "99D9D9",
	"STL": "002F87", "TBL": "002868", "TOR": "00205B", "UTA": "6CACE4",
	"VAN": "00843D", "VGK": "B4975A", "WSH": "C8102E", "WPG": "041E42",
}

// TeamColor returns a club's primary colour, or neutral grey for a club
// the table does not know (expansion, relocation, or a preseason split
// squad code).
func TeamColor(abbrev string) string {
	if c, ok := teamColors[abbrev]; ok {
		return c
	}
	return "888888"
}
```

- [ ] **Step 4: Run tests, commit**

Run: `cd cloud && go test ./internal/reduce/ -v`
Expected: PASS.

```bash
git add cloud/internal/reduce && git commit -m "reduce: team colour table"
```

---

### Task 5: Goal and roster folds

**Files:**
- Modify: `cloud/internal/reduce/reduce.go` (replace the `applyPlay` stub)
- Test: append to `cloud/internal/reduce/reduce_test.go`

**Interfaces:**
- Consumes: `playDetail`, `s.Roster`, `s.Situation.PP`.
- Produces: `func (s State) applyPlay(e Event) (State, bool, error)` — dedupes on `Seq` (ignores `seq <= s.LastSeq`), handles `goal`, `penalty` (Task 6), `period-start`, `period-end`; everything else advances `LastSeq` and returns unchanged.

- [ ] **Step 1: Write the failing tests**

```go
func TestGoalUpdatesScoreAndLastGoalWithNumber(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.roster", "roster.json"))
	s, changed, err := Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.Away.Abbrev != "CHI" || s.Home.Abbrev != "FLA" {
		t.Errorf("play event must fix home/away: %+v %+v", s.Away, s.Home)
	}
	if s.Away.Score != 1 || s.Home.Score != 0 {
		t.Errorf("score = %d-%d", s.Away.Score, s.Home.Score)
	}
	if s.LastGoal == nil || s.LastGoal.Team != "CHI" || s.LastGoal.Number != 91 || s.LastGoal.AsOf == 0 {
		t.Errorf("lastGoal = %+v", s.LastGoal)
	}
	if s.LastSeq != 166 {
		t.Errorf("lastSeq = %d", s.LastSeq)
	}
}

func TestDuplicatePlayIsIgnored(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	s2, changed, _ := Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	if changed || s2.Away.Score != 1 {
		t.Errorf("duplicate applied: changed=%v score=%d", changed, s2.Away.Score)
	}
}

func TestGoalWithoutRosterHasNumberZero(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "goal_chi.json"))
	if s.LastGoal == nil || s.LastGoal.Number != 0 {
		t.Errorf("lastGoal = %+v, want number 0 when the roster is unknown", s.LastGoal)
	}
}

func TestPeriodStartLearnsPeriodAndClearsIntermission(t *testing.T) {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.clock", "clock_intermission.json"))
	ps := Event{DetailType: "nhl.game.play", Time: time.Now(), Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":400,"playType":"period-start","homeTeam":"FLA","awayTeam":"CHI","period":2,"timeInPeriod":"00:00","score":{"CHI":1,"FLA":0},"raw":{"periodDescriptor":{"number":2,"periodType":"REG"},"typeDescKey":"period-start","details":{}}}`)}
	s, changed, err := Reduce(s, ps)
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if s.Period.Number != 2 || s.Period.Label != "2" || s.Clock.Intermission {
		t.Errorf("after period-start: %+v %+v", s.Period, s.Clock)
	}
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd cloud && go test ./internal/reduce/ -run 'Goal|Duplicate|PeriodStart' -v`
Expected: FAIL (stub returns unchanged).

- [ ] **Step 3: Implement `applyPlay`** (replace the stub in `reduce.go`)

```go
func (s State) applyPlay(e Event) (State, bool, error) {
	var d playDetail
	if err := json.Unmarshal(e.Detail, &d); err != nil {
		return s, false, fmt.Errorf("play: %w", err)
	}
	if d.Seq <= s.LastSeq {
		return s, false, nil // at-least-once delivery: already folded
	}
	s.LastSeq = d.Seq
	s.GameID = d.GameID
	s.setTeams(d.AwayTeam, d.HomeTeam)
	s.applyScore(d.Score)
	stamp := func() { s.AsOf = e.Time.UTC().UnixMilli() }

	switch d.PlayType {
	case "goal":
		team := d.ScoringTeam
		s.LastGoal = &Goal{Team: team, Number: s.Roster[d.Raw.Details.ScoringPlayerID], AsOf: e.Time.UTC().UnixMilli()}
		s.endMinorOnPowerPlayGoal(team)
		stamp()
		return s, true, nil
	case "penalty":
		s.addPenalty(d)
		stamp()
		return s, true, nil
	case "period-start":
		n, typ := d.Raw.PeriodDescriptor.Number, d.Raw.PeriodDescriptor.PeriodType
		if n == 0 {
			n = d.Period
		}
		s.Period = Period{Number: n, Type: typ, Label: PeriodLabel(n, typ)}
		s.Clock.Intermission = false
		stamp()
		return s, true, nil
	case "period-end":
		s.Clock.Running = false
		stamp()
		return s, true, nil
	}
	return s, false, nil
}
```

Add temporary stubs (removed in Task 6):
```go
func (s *State) addPenalty(d playDetail)               {}
func (s *State) endMinorOnPowerPlayGoal(scoring string) {}
```

- [ ] **Step 4: Run tests, commit**

Run: `cd cloud && go test ./internal/reduce/ -v`
Expected: PASS.

```bash
git add cloud/internal/reduce && git commit -m "reduce: goal, roster and period folds with seq dedupe"
```

---

### Task 6: Penalty box

**Files:**
- Create: `cloud/internal/reduce/penalties.go` (and delete the three stubs from `reduce.go`)
- Test: `cloud/internal/reduce/penalties_test.go`

**Interfaces:**
- Produces: `func (s *State) addPenalty(d playDetail)`, `func (s *State) tickPenalties()`, `func (s *State) endMinorOnPowerPlayGoal(scoringTeam string)`.
- Rules (spec §3.3): MIN and BEN are 2-minute (or `duration`) penalties that end early on a power-play goal against; MAJ (5) and MAT/MIS (10) do not; PS (penalty shot) and unknown types are not boxed. Bench minors use `servedByPlayerId` for the number. Remaining time is game time: `Duration − (nowT − StartT)`, where `nowT` comes from the latest clock heartbeat; penalties at or below 0 are dropped. Only the PP-side's oldest ending-on-goal penalty ends on a goal, and only when `s.Situation.PP == scoringTeam`.

- [ ] **Step 1: Write the failing tests**

`penalties_test.go`:
```go
package reduce

import (
	"encoding/json"
	"testing"
	"time"
)

func clockAt(period, remaining int, code string, obs time.Time) Event {
	b, _ := json.Marshal(map[string]any{"schemaVersion": 1, "gameId": 2025020001, "gameState": "LIVE", "period": period, "periodType": "REG",
		"secondsRemaining": remaining, "timeRemaining": "x", "running": true, "inIntermission": false, "situationCode": code,
		"homeTeam": "FLA", "awayTeam": "CHI", "score": map[string]int{"CHI": 0, "FLA": 0}, "shots": map[string]int{"CHI": 1, "FLA": 1}, "observedAt": obs})
	return Event{DetailType: "nhl.game.clock", Detail: b, Time: obs}
}

func liveWithRoster(t *testing.T) State {
	s, _, _ := Reduce(State{}, load(t, "nhl.game.status", "status_live.json"))
	s, _, _ = Reduce(s, load(t, "nhl.game.roster", "roster.json"))
	return s
}

func TestMinorPenaltyIsBoxedWithNumberAndTicksDown(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 782, "1551", t0)) // 13:02 left when the penalty is called
	s, changed, err := Reduce(s, load(t, "nhl.game.play", "penalty_chi_min.json")) // 06:58 elapsed in P1
	if err != nil || !changed {
		t.Fatalf("err=%v changed=%v", err, changed)
	}
	if len(s.Penalties) != 1 {
		t.Fatalf("penalties = %+v", s.Penalties)
	}
	p := s.Penalties[0]
	if p.Team != "CHI" || p.Number != 81 || p.Type != "MIN" || p.Seconds != 120 || !p.EndsOnGoal {
		t.Errorf("penalty = %+v", p)
	}
	// 45 game-seconds later (12:17 left in P1) the clock heartbeat ticks it.
	s, _, _ = Reduce(s, clockAt(1, 737, "1451", t0.Add(45*time.Second)))
	if s.Penalties[0].Seconds != 75 {
		t.Errorf("remaining = %d, want 75", s.Penalties[0].Seconds)
	}
	if s.Situation.PP != "FLA" {
		t.Errorf("pp = %q", s.Situation.PP)
	}
	// Expiry: 2:00 after 06:58 is 08:58 elapsed → 11:02 remaining.
	s, _, _ = Reduce(s, clockAt(1, 662, "1551", t0.Add(120*time.Second)))
	if len(s.Penalties) != 0 {
		t.Errorf("penalty should have expired: %+v", s.Penalties)
	}
}

func TestPenaltySpansIntermission(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 40, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 30, "1551", t0))
	pen := load(t, "nhl.game.play", "penalty_chi_min.json")
	// Rewrite the fixture's time to 19:30 elapsed in P1.
	var m map[string]any
	json.Unmarshal(pen.Detail, &m)
	m["timeInPeriod"] = "19:30"
	m["seq"] = float64(900)
	pen.Detail, _ = json.Marshal(m)
	s, _, _ = Reduce(s, pen)
	// First heartbeat of P2 at 19:00 remaining: 30 s of P1 + 60 s of P2 = 90 s served.
	s, _, _ = Reduce(s, clockAt(2, 1140, "1451", t0.Add(20*time.Minute)))
	if len(s.Penalties) != 1 || s.Penalties[0].Seconds != 30 {
		t.Errorf("penalties = %+v, want one with 30 s left", s.Penalties)
	}
}

func TestPowerPlayGoalEndsOldestMinor(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 782, "1551", t0))
	s, _, _ = Reduce(s, load(t, "nhl.game.play", "penalty_chi_min.json"))
	s, _, _ = Reduce(s, clockAt(1, 760, "1451", t0.Add(22*time.Second))) // FLA on the PP
	// FLA scores.
	goal := Event{DetailType: "nhl.game.play", Time: t0.Add(30 * time.Second), Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":300,"playType":"goal","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"FLA","scoringTeam":"FLA","period":1,"timeInPeriod":"07:30","score":{"CHI":0,"FLA":1},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"goal","details":{"scoringPlayerId":8473419,"eventOwnerTeamId":13}}}`)}
	s, _, _ = Reduce(s, goal)
	if len(s.Penalties) != 0 {
		t.Errorf("minor should end on the PP goal: %+v", s.Penalties)
	}
	if s.LastGoal == nil || s.LastGoal.Number != 63 {
		t.Errorf("lastGoal = %+v", s.LastGoal)
	}
}

func TestMajorSurvivesGoalAndBenchMinorUsesServer(t *testing.T) {
	s := liveWithRoster(t)
	t0 := time.Date(2025, 10, 7, 21, 20, 0, 0, time.UTC)
	s, _, _ = Reduce(s, clockAt(1, 782, "1551", t0))
	major := Event{DetailType: "nhl.game.play", Time: t0, Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":126,"playType":"penalty","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","period":1,"timeInPeriod":"06:58","score":{"CHI":0,"FLA":0},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"penalty","details":{"typeCode":"MAJ","duration":5,"committedByPlayerId":8483493,"eventOwnerTeamId":16}}}`)}
	bench := Event{DetailType: "nhl.game.play", Time: t0, Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":127,"playType":"penalty","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","period":1,"timeInPeriod":"06:58","score":{"CHI":0,"FLA":0},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"penalty","details":{"typeCode":"BEN","duration":2,"servedByPlayerId":8484783,"eventOwnerTeamId":16}}}`)}
	ps := Event{DetailType: "nhl.game.play", Time: t0, Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":128,"playType":"penalty","homeTeam":"FLA","awayTeam":"CHI","actingTeam":"CHI","period":1,"timeInPeriod":"06:58","score":{"CHI":0,"FLA":0},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"penalty","details":{"typeCode":"PS","duration":0,"committedByPlayerId":8483493,"eventOwnerTeamId":16}}}`)}
	for _, e := range []Event{major, bench, ps} {
		s, _, _ = Reduce(s, e)
	}
	if len(s.Penalties) != 2 {
		t.Fatalf("penalties = %+v, want MAJ + BEN only", s.Penalties)
	}
	if s.Penalties[0].Type != "MAJ" || s.Penalties[0].Seconds != 300 || s.Penalties[0].EndsOnGoal || s.Penalties[0].Number != 91 {
		t.Errorf("major = %+v", s.Penalties[0])
	}
	if s.Penalties[1].Type != "BEN" || s.Penalties[1].Number != 81 || !s.Penalties[1].EndsOnGoal {
		t.Errorf("bench minor = %+v", s.Penalties[1])
	}
	s, _, _ = Reduce(s, clockAt(1, 770, "1351", t0.Add(12*time.Second)))
	goal := Event{DetailType: "nhl.game.play", Time: t0.Add(15 * time.Second), Detail: json.RawMessage(`{"schemaVersion":1,"gameId":2025020001,"seq":300,"playType":"goal","homeTeam":"FLA","awayTeam":"CHI","scoringTeam":"FLA","period":1,"timeInPeriod":"07:13","score":{"CHI":0,"FLA":1},"raw":{"periodDescriptor":{"number":1,"periodType":"REG"},"typeDescKey":"goal","details":{"scoringPlayerId":8473419,"eventOwnerTeamId":13}}}`)}
	s, _, _ = Reduce(s, goal)
	if len(s.Penalties) != 1 || s.Penalties[0].Type != "MAJ" {
		t.Errorf("after PP goal want only the major left: %+v", s.Penalties)
	}
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd cloud && go test ./internal/reduce/ -run 'Penalty|PowerPlay|Major' -v`
Expected: FAIL (stubs do nothing).

- [ ] **Step 3: Implement `penalties.go`** and remove the `addPenalty`, `endMinorOnPowerPlayGoal`, `tickPenalties` stubs from `reduce.go`

```go
package reduce

import (
	"sort"
	"strconv"
	"strings"
)

// parseClock turns "06:58" into 418 seconds.
func parseClock(mmss string) int {
	parts := strings.SplitN(mmss, ":", 2)
	if len(parts) != 2 {
		return 0
	}
	m, _ := strconv.Atoi(parts[0])
	sec, _ := strconv.Atoi(parts[1])
	return m*60 + sec
}

// nowT is the absolute game time of the latest clock heartbeat.
func (s *State) nowT() int {
	elapsed := PeriodLength(s.Period.Number, s.OTLen) - s.Clock.Seconds
	if elapsed < 0 {
		elapsed = 0
	}
	return GameTime(s.Period.Number, elapsed, s.OTLen)
}

// addPenalty boxes a penalty play. Penalty shots and unknown codes are
// not boxed; misconducts are shown but never end on a goal.
func (s *State) addPenalty(d playDetail) {
	det := d.Raw.Details
	var endsOnGoal bool
	switch det.TypeCode {
	case "MIN", "BEN":
		endsOnGoal = true
	case "MAJ", "MAT", "MIS":
		endsOnGoal = false
	default:
		return
	}
	if det.Duration <= 0 {
		return
	}
	pid := det.CommittedByPlayerID
	if det.TypeCode == "BEN" || pid == 0 {
		pid = det.ServedByPlayerID
	}
	period := d.Raw.PeriodDescriptor.Number
	if period == 0 {
		period = d.Period
	}
	if period > s.Period.Number { // a play from a period we have not seen a heartbeat for yet
		s.Period = Period{Number: period, Type: d.Raw.PeriodDescriptor.PeriodType, Label: PeriodLabel(period, d.Raw.PeriodDescriptor.PeriodType)}
	}
	start := GameTime(period, parseClock(d.TimeInPeriod), s.OTLen)
	s.Penalties = append(s.Penalties, Penalty{
		Team: d.ActingTeam, Number: s.Roster[pid], Type: det.TypeCode,
		Seconds: det.Duration * 60, Duration: det.Duration * 60, EndsOnGoal: endsOnGoal, StartT: start,
	})
	sort.SliceStable(s.Penalties, func(i, j int) bool { return s.Penalties[i].StartT < s.Penalties[j].StartT })
}

// tickPenalties recomputes remaining time from the current game clock and
// drops anything that has expired.
func (s *State) tickPenalties() {
	now := s.nowT()
	kept := s.Penalties[:0]
	for _, p := range s.Penalties {
		remaining := p.Duration - (now - p.StartT)
		if remaining > p.Duration {
			remaining = p.Duration // clock ran backwards (correction); never exceed the sentence
		}
		if remaining <= 0 {
			continue
		}
		p.Seconds = remaining
		kept = append(kept, p)
	}
	s.Penalties = kept
}

// endMinorOnPowerPlayGoal releases the shorthanded side's oldest minor when
// the team on the power play scores. The situation code decides who was on
// the power play; if it says nobody, nothing is released.
func (s *State) endMinorOnPowerPlayGoal(scoringTeam string) {
	if s.Situation.PP != scoringTeam {
		return
	}
	for i, p := range s.Penalties {
		if p.Team != scoringTeam && p.EndsOnGoal {
			s.Penalties = append(s.Penalties[:i], s.Penalties[i+1:]...)
			return
		}
	}
}
```

- [ ] **Step 4: Run the package**

Run: `cd cloud && go test ./internal/reduce/ -v`
Expected: PASS (all tests, including the earlier ones). Arithmetic check for `TestPenaltySpansIntermission`: the penalty at 19:30 of P1 starts at game time 1170; the P2 heartbeat at 19:00 remaining is game time 1200 + 60 = 1260; 90 s served, 30 s left.

- [ ] **Step 5: gofmt/vet, commit**

```bash
cd cloud && gofmt -l . && go vet ./... && cd .. && git add cloud/internal/reduce && git commit -m "reduce: penalty box with game-clock expiry and power-play-goal release"
```

---

### Task 7: Store and publisher interfaces with fakes and AWS implementations

**Files:**
- Create: `cloud/internal/gamestore/store.go`, `cloud/internal/gamestore/dynamo.go`, `cloud/internal/iotpub/publisher.go`
- Test: `cloud/internal/gamestore/store_test.go`
- Modify: `cloud/go.mod` (add `github.com/aws/aws-sdk-go-v2`, `.../config`, `.../service/dynamodb`, `.../feature/dynamodb/attributevalue`, `.../service/iotdataplane`, `github.com/aws/aws-lambda-go`)

**Interfaces:**
- Produces:
```go
package gamestore
type Store interface {
	Get(ctx context.Context, gameID int64) (reduce.State, bool, error)       // found?
	Put(ctx context.Context, s reduce.State) error                            // conditional on s.Version == stored version (0 = new); increments Version; returns ErrConflict
	ListActive(ctx context.Context) ([]reduce.State, error)                   // states with GameState != "FINAL" or updated in last 6h (for the today doc)
}
var ErrConflict = errors.New("gamestore: version conflict")
type Fake struct{ ... } ; func NewFake() *Fake
func NewDynamo(client *dynamodb.Client, table string) *Dynamo
```
```go
package iotpub
type Publisher interface { Publish(ctx context.Context, topic string, payload []byte, retain bool) error }
type Fake struct{ Messages []Message } ; type Message struct{ Topic string; Payload []byte; Retain bool }
func NewIoT(client *iotdataplane.Client) *IoT
```

- [ ] **Step 1: Write the failing test** (`store_test.go`, exercising the fake's conditional write, which the DynamoDB implementation mirrors)

```go
package gamestore

import (
	"context"
	"errors"
	"testing"

	"hockeytrack-scoreboard/internal/reduce"
)

func TestFakeStoreOptimisticLock(t *testing.T) {
	st := NewFake()
	ctx := context.Background()
	if _, found, _ := st.Get(ctx, 1); found {
		t.Fatal("empty store reported a game")
	}
	s := reduce.State{V: 1, GameID: 1, GameState: "LIVE"}
	if err := st.Put(ctx, s); err != nil {
		t.Fatal(err)
	}
	got, found, _ := st.Get(ctx, 1)
	if !found || got.Version != 1 {
		t.Fatalf("after first put: found=%v version=%d", found, got.Version)
	}
	stale := s // Version 0
	if err := st.Put(ctx, stale); !errors.Is(err, ErrConflict) {
		t.Errorf("stale put err = %v, want ErrConflict", err)
	}
	got.GameState = "FINAL"
	if err := st.Put(ctx, got); err != nil {
		t.Fatal(err)
	}
	got2, _, _ := st.Get(ctx, 1)
	if got2.Version != 2 || got2.GameState != "FINAL" {
		t.Errorf("after second put: %+v", got2)
	}
	active, _ := st.ListActive(ctx)
	if len(active) != 1 {
		t.Errorf("ListActive = %d items", len(active))
	}
}
```

- [ ] **Step 2: Run to verify failure**

Run: `cd cloud && go test ./internal/gamestore/ -v`
Expected: FAIL to compile.

- [ ] **Step 3: Implement the interface and fake** (`store.go`)

```go
package gamestore

import (
	"context"
	"errors"
	"sync"

	"hockeytrack-scoreboard/internal/reduce"
)

var ErrConflict = errors.New("gamestore: version conflict")

// Store persists one reduce.State per game with an optimistic lock on
// State.Version so concurrent reducer invocations converge.
type Store interface {
	Get(ctx context.Context, gameID int64) (reduce.State, bool, error)
	Put(ctx context.Context, s reduce.State) error
	ListActive(ctx context.Context) ([]reduce.State, error)
}

type Fake struct {
	mu    sync.Mutex
	items map[int64]reduce.State
}

func NewFake() *Fake { return &Fake{items: map[int64]reduce.State{}} }

func (f *Fake) Get(_ context.Context, gameID int64) (reduce.State, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.items[gameID]
	return s, ok, nil
}

func (f *Fake) Put(_ context.Context, s reduce.State) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if cur, ok := f.items[s.GameID]; ok && cur.Version != s.Version {
		return ErrConflict
	} else if !ok && s.Version != 0 {
		return ErrConflict
	}
	s.Version++
	f.items[s.GameID] = s
	return nil
}

func (f *Fake) ListActive(_ context.Context) ([]reduce.State, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []reduce.State
	for _, s := range f.items {
		out = append(out, s)
	}
	return out, nil
}
```

`dynamo.go` (table: partition key `gameId` (N); attribute `expiresAt` (N) for TTL; `Put` uses `attributevalue.MarshalMap` on the State, sets `expiresAt = now + 48h`, and a `ConditionExpression`: `attribute_not_exists(gameId)` when `s.Version == 0`, else `version = :expected`; map `ConditionalCheckFailedException` to `ErrConflict`. `ListActive` scans with `FilterExpression: #st <> :final OR asOf > :cutoff` (`:cutoff` = now − 6h in ms); the table is tiny (tens of items) so a scan is fine.)

```go
package gamestore

import (
	"context"
	"errors"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"

	"hockeytrack-scoreboard/internal/reduce"
)

type Dynamo struct {
	client *dynamodb.Client
	table  string
	now    func() time.Time
}

func NewDynamo(client *dynamodb.Client, table string) *Dynamo {
	return &Dynamo{client: client, table: table, now: time.Now}
}

type item struct {
	reduce.State
	ExpiresAt int64 `dynamodbav:"expiresAt"`
}

func (d *Dynamo) Get(ctx context.Context, gameID int64) (reduce.State, bool, error) {
	out, err := d.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName:      aws.String(d.table),
		Key:            map[string]types.AttributeValue{"gameId": &types.AttributeValueMemberN{Value: itoa(gameID)}},
		ConsistentRead: aws.Bool(true),
	})
	if err != nil {
		return reduce.State{}, false, err
	}
	if out.Item == nil {
		return reduce.State{}, false, nil
	}
	var it item
	if err := attributevalue.UnmarshalMap(out.Item, &it); err != nil {
		return reduce.State{}, false, err
	}
	return it.State, true, nil
}

func (d *Dynamo) Put(ctx context.Context, s reduce.State) error {
	expected := s.Version
	s.Version++
	av, err := attributevalue.MarshalMap(item{State: s, ExpiresAt: d.now().Add(48 * time.Hour).Unix()})
	if err != nil {
		return err
	}
	in := &dynamodb.PutItemInput{TableName: aws.String(d.table), Item: av}
	if expected == 0 {
		in.ConditionExpression = aws.String("attribute_not_exists(gameId)")
	} else {
		in.ConditionExpression = aws.String("version = :v")
		in.ExpressionAttributeValues = map[string]types.AttributeValue{":v": &types.AttributeValueMemberN{Value: itoa(expected)}}
	}
	if _, err := d.client.PutItem(ctx, in); err != nil {
		var ccf *types.ConditionalCheckFailedException
		if errors.As(err, &ccf) {
			return ErrConflict
		}
		return err
	}
	return nil
}

func (d *Dynamo) ListActive(ctx context.Context) ([]reduce.State, error) {
	cutoff := d.now().Add(-6 * time.Hour).UnixMilli()
	p := dynamodb.NewScanPaginator(d.client, &dynamodb.ScanInput{
		TableName:                aws.String(d.table),
		FilterExpression:         aws.String("#st <> :final OR asOf > :cutoff"),
		ExpressionAttributeNames: map[string]string{"#st": "state"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":final":  &types.AttributeValueMemberS{Value: "FINAL"},
			":cutoff": &types.AttributeValueMemberN{Value: itoa(cutoff)},
		},
	})
	var out []reduce.State
	for p.HasMorePages() {
		page, err := p.NextPage(ctx)
		if err != nil {
			return nil, err
		}
		for _, raw := range page.Items {
			var it item
			if err := attributevalue.UnmarshalMap(raw, &it); err != nil {
				return nil, err
			}
			out = append(out, it.State)
		}
	}
	return out, nil
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }
```

Add `"strconv"` to the imports. The `item` embedding requires `reduce.State`'s `dynamodbav` tags from Task 2; the map field `Roster map[int64]int` marshals as a DynamoDB map with numeric-string keys, which `attributevalue` supports.

`publisher.go`:
```go
package iotpub

import (
	"context"
	"sync"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"
)

type Publisher interface {
	Publish(ctx context.Context, topic string, payload []byte, retain bool) error
}

type Message struct {
	Topic   string
	Payload []byte
	Retain  bool
}

type Fake struct {
	mu       sync.Mutex
	Messages []Message
}

func (f *Fake) Publish(_ context.Context, topic string, payload []byte, retain bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Messages = append(f.Messages, Message{Topic: topic, Payload: append([]byte(nil), payload...), Retain: retain})
	return nil
}

type IoT struct{ client *iotdataplane.Client }

func NewIoT(client *iotdataplane.Client) *IoT { return &IoT{client: client} }

func (p *IoT) Publish(ctx context.Context, topic string, payload []byte, retain bool) error {
	_, err := p.client.Publish(ctx, &iotdataplane.PublishInput{
		Topic:       aws.String(topic),
		Payload:     payload,
		Qos:         1,
		Retain:      retain,
		ContentType: aws.String("application/json"),
	})
	return err
}
```

- [ ] **Step 4: Add dependencies and run**

```bash
cd cloud && go get github.com/aws/aws-sdk-go-v2@latest github.com/aws/aws-sdk-go-v2/config@latest github.com/aws/aws-sdk-go-v2/service/dynamodb@latest github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue@latest github.com/aws/aws-sdk-go-v2/service/iotdataplane@latest github.com/aws/aws-lambda-go@latest && go mod tidy && go build ./... && go vet ./... && go test ./...
```
Expected: builds; `gamestore` test PASS.

- [ ] **Step 5: Commit**

```bash
git add cloud && git commit -m "store and publisher interfaces with DynamoDB and IoT Data Plane implementations"
```

---

### Task 8: The reducer Lambda

**Files:**
- Create: `cloud/cmd/reducer/main.go`, `cloud/cmd/reducer/handler.go`
- Test: `cloud/cmd/reducer/handler_test.go`

**Interfaces:**
- Consumes: `reduce.Reduce`, `gamestore.Store`, `iotpub.Publisher`.
- Produces: `type Handler struct{ Store gamestore.Store; Pub iotpub.Publisher; TopicPrefix string }`; `func (h Handler) Handle(ctx context.Context, ev events.CloudWatchEvent) error` (from `github.com/aws/aws-lambda-go/events`). Topic: `TopicPrefix + "/" + gameId + "/state"` with `TopicPrefix = "hockeytrack/games"`. On `ErrConflict`, re-read and retry once; on a second conflict return the error so Lambda retries the event.

- [ ] **Step 1: Write the failing test**

```go
package main

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
)

func ebEvent(t *testing.T, detailType, file string) events.CloudWatchEvent {
	t.Helper()
	b, err := os.ReadFile("../../internal/reduce/testdata/events/" + file)
	if err != nil {
		t.Fatal(err)
	}
	return events.CloudWatchEvent{Source: "hockeytrack.poller", DetailType: detailType, Detail: json.RawMessage(b), Time: time.Date(2025, 10, 7, 22, 31, 6, 0, time.UTC)}
}

func TestHandlerPublishesRetainedStateOnChange(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: st, Pub: pub, TopicPrefix: "hockeytrack/games"}
	ctx := context.Background()

	if err := h.Handle(ctx, ebEvent(t, "nhl.game.status", "status_live.json")); err != nil {
		t.Fatal(err)
	}
	if err := h.Handle(ctx, ebEvent(t, "nhl.game.roster", "roster.json")); err != nil {
		t.Fatal(err) // roster changes nothing visible: no publish
	}
	if err := h.Handle(ctx, ebEvent(t, "nhl.game.clock", "clock_p2.json")); err != nil {
		t.Fatal(err)
	}
	if len(pub.Messages) != 2 {
		t.Fatalf("published %d messages, want 2 (status, clock)", len(pub.Messages))
	}
	m := pub.Messages[1]
	if m.Topic != "hockeytrack/games/2025020001/state" || !m.Retain {
		t.Errorf("message = %+v", m)
	}
	if !strings.Contains(string(m.Payload), `"sog":17`) || strings.Contains(string(m.Payload), "lastSeq") {
		t.Errorf("payload = %s", m.Payload)
	}
	s, found, _ := st.Get(ctx, 2025020001)
	if !found || s.Version != 3 || len(s.Roster) != 4 {
		t.Errorf("stored = found:%v version:%d roster:%d", found, s.Version, len(s.Roster))
	}
}

func TestHandlerRetriesOnceOnConflict(t *testing.T) {
	st, pub := gamestore.NewFake(), &iotpub.Fake{}
	h := Handler{Store: st, Pub: pub, TopicPrefix: "hockeytrack/games"}
	ctx := context.Background()
	_ = h.Handle(ctx, ebEvent(t, "nhl.game.status", "status_live.json"))
	// Simulate a concurrent writer bumping the version between Get and Put.
	conflicting := &conflictOnce{Store: st}
	h.Store = conflicting
	if err := h.Handle(ctx, ebEvent(t, "nhl.game.clock", "clock_p2.json")); err != nil {
		t.Fatalf("expected the retry to succeed: %v", err)
	}
	if conflicting.puts != 2 {
		t.Errorf("puts = %d, want 2 (conflict then success)", conflicting.puts)
	}
}

// conflictOnce wraps a Store and fails the first Put with ErrConflict.
type conflictOnce struct {
	gamestore.Store
	puts int
}

func (c *conflictOnce) Put(ctx context.Context, s reduce.State) error {
	c.puts++
	if c.puts == 1 {
		return gamestore.ErrConflict
	}
	return c.Store.Put(ctx, s)
}
```

Add `"hockeytrack-scoreboard/internal/reduce"` to the test's imports.

- [ ] **Step 2: Run to verify failure**

Run: `cd cloud && go test ./cmd/reducer/ -v`
Expected: FAIL to compile (`Handler` undefined).

- [ ] **Step 3: Implement `handler.go` and `main.go`**

`handler.go`:
```go
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/reduce"
)

type Handler struct {
	Store       gamestore.Store
	Pub         iotpub.Publisher
	TopicPrefix string
}

type gameIDOnly struct {
	GameID int64 `json:"gameId"`
}

// Handle folds one bus event into the game's state and republishes the
// retained document when it changed. A version conflict (another
// invocation wrote first) is retried once from a fresh read.
func (h Handler) Handle(ctx context.Context, ev events.CloudWatchEvent) error {
	var id gameIDOnly
	if err := json.Unmarshal(ev.Detail, &id); err != nil || id.GameID == 0 {
		return fmt.Errorf("event %s has no gameId: %v", ev.DetailType, err)
	}
	for attempt := 0; attempt < 2; attempt++ {
		cur, _, err := h.Store.Get(ctx, id.GameID)
		if err != nil {
			return err
		}
		next, changed, err := reduce.Reduce(cur, reduce.Event{DetailType: ev.DetailType, Detail: ev.Detail, Time: ev.Time})
		if err != nil {
			return err
		}
		if err := h.Store.Put(ctx, next); err != nil {
			if errors.Is(err, gamestore.ErrConflict) && attempt == 0 {
				slog.Warn("version conflict; retrying", "gameId", id.GameID)
				continue
			}
			return err
		}
		if !changed {
			return nil
		}
		body, err := next.JSON()
		if err != nil {
			return err
		}
		topic := h.TopicPrefix + "/" + strconv.FormatInt(id.GameID, 10) + "/state"
		return h.Pub.Publish(ctx, topic, body, true)
	}
	return gamestore.ErrConflict
}
```
`main.go`:
```go
package main

import (
	"context"
	"log/slog"
	"os"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
)

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	table := os.Getenv("GAMES_TABLE")
	endpoint := os.Getenv("IOT_ENDPOINT") // https://xxxx-ats.iot.us-east-1.amazonaws.com
	if table == "" || endpoint == "" {
		slog.Error("GAMES_TABLE and IOT_ENDPOINT are required")
		os.Exit(1)
	}
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	h := Handler{
		Store:       gamestore.NewDynamo(dynamodb.NewFromConfig(cfg), table),
		Pub:         iotpub.NewIoT(iot),
		TopicPrefix: "hockeytrack/games",
	}
	lambda.Start(h.Handle)
}
```

- [ ] **Step 4: Run tests, commit**

Run: `cd cloud && go vet ./... && go test ./...`
Expected: PASS.

```bash
git add cloud/cmd/reducer && git commit -m "reducer Lambda: fold bus events, persist with optimistic lock, publish retained state"
```

---

### Task 9: The "today" document and Lambda

**Files:**
- Create: `cloud/internal/today/today.go`, `cloud/internal/today/testdata/schedule.json` (copy of `https://hockeytrack.davidjdrake.com/data/schedule.json`, fetched once with curl), `cloud/cmd/today/main.go`
- Test: `cloud/internal/today/today_test.go`

**Interfaces:**
- Produces:
```go
package today
type Game struct { GameID int64 `json:"gameId"`; Away, Home string; Start string; State string }   // json: gameId, away, home, start, state
type Doc struct { GeneratedAt int64 `json:"generatedAt"`; Games []Game `json:"games"` }
func Build(schedule []byte, states map[int64]string, now time.Time) (Doc, error)
```
- Rules (spec §8, proposal accepted): include games dated today in ET, plus games dated yesterday whose state is not FINAL, until noon ET. `State` is the reducer's stored state if present, else `"PRE"`. Sorted by start time.

- [ ] **Step 1: Write the failing test**

```go
package today

import (
	"os"
	"testing"
	"time"
)

func TestBuildPicksTodayAndCarriesLateGames(t *testing.T) {
	sched, err := os.ReadFile("testdata/schedule.json")
	if err != nil {
		t.Fatal(err)
	}
	// 2026-09-20 09:00 ET: the day after the preseason opener.
	now := time.Date(2026, 9, 20, 13, 0, 0, 0, time.UTC)
	doc, err := Build(sched, map[int64]string{2026010001: "FINAL", 2026010007: "LIVE"}, now)
	if err != nil {
		t.Fatal(err)
	}
	var today, yesterday int
	for _, g := range doc.Games {
		switch {
		case g.Start >= "2026-09-20T04:00:00Z" && g.Start < "2026-09-21T04:00:00Z":
			today++
		default:
			yesterday++
			if g.State == "FINAL" {
				t.Errorf("finished game from yesterday should not be listed: %+v", g)
			}
		}
	}
	if today == 0 {
		t.Error("no games for 2026-09-20 found")
	}
	if yesterday != 1 {
		t.Errorf("late/unfinished games from yesterday = %d, want 1 (game 2026010007 LIVE)", yesterday)
	}
	for i := 1; i < len(doc.Games); i++ {
		if doc.Games[i].Start < doc.Games[i-1].Start {
			t.Fatal("games not sorted by start")
		}
	}
	// After noon ET, yesterday's games drop even if not FINAL in our table.
	doc, _ = Build(sched, map[int64]string{2026010007: "LIVE"}, time.Date(2026, 9, 20, 17, 0, 0, 0, time.UTC))
	for _, g := range doc.Games {
		if g.GameID == 2026010007 {
			t.Error("yesterday's game still listed after noon ET")
		}
	}
	if doc.GeneratedAt == 0 {
		t.Error("generatedAt unset")
	}
}
```

Fetch the fixture first: `curl -s https://hockeytrack.davidjdrake.com/data/schedule.json -o cloud/internal/today/testdata/schedule.json` and confirm game 2026010007 is dated 2026-09-19 (`python3 -c "import json;d=json.load(open('cloud/internal/today/testdata/schedule.json'));print([g for g in d['games'] if g['id']==2026010007])"`). If it is not, pick any 2026-09-19 game id from that output and use it in the test.

- [ ] **Step 2: Run to verify failure**

Run: `cd cloud && go test ./internal/today/ -v`
Expected: FAIL to compile.

- [ ] **Step 3: Implement `today.go`**

```go
package today

import (
	"encoding/json"
	"sort"
	"time"
)

type Game struct {
	GameID int64  `json:"gameId"`
	Away   string `json:"away"`
	Home   string `json:"home"`
	Start  string `json:"start"`
	State  string `json:"state"`
}

type Doc struct {
	GeneratedAt int64  `json:"generatedAt"`
	Games       []Game `json:"games"`
}

type schedule struct {
	Games []struct {
		ID    int64  `json:"id"`
		Date  string `json:"date"`
		Start string `json:"start"`
		Away  string `json:"away"`
		Home  string `json:"home"`
	} `json:"games"`
}

var eastern = mustLoad("America/New_York")

func mustLoad(name string) *time.Location {
	l, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return l
}

// Build lists today's games (ET) plus yesterday's unfinished ones until
// noon ET, with each game's current reducer state (PRE when unknown).
func Build(scheduleJSON []byte, states map[int64]string, now time.Time) (Doc, error) {
	var s schedule
	if err := json.Unmarshal(scheduleJSON, &s); err != nil {
		return Doc{}, err
	}
	et := now.In(eastern)
	today := et.Format("2006-01-02")
	yesterday := et.AddDate(0, 0, -1).Format("2006-01-02")
	carryYesterday := et.Hour() < 12
	doc := Doc{GeneratedAt: now.UnixMilli(), Games: []Game{}}
	for _, g := range s.Games {
		state := states[g.ID]
		if state == "" {
			state = "PRE"
		}
		switch {
		case g.Date == today:
		case g.Date == yesterday && carryYesterday && state != "FINAL":
		default:
			continue
		}
		doc.Games = append(doc.Games, Game{GameID: g.ID, Away: g.Away, Home: g.Home, Start: g.Start, State: state})
	}
	sort.Slice(doc.Games, func(i, j int) bool { return doc.Games[i].Start < doc.Games[j].Start })
	return doc, nil
}
```

Note: Lambda's `provided.al2023` image has no tzdata; import `_ "time/tzdata"` in `cmd/today/main.go` so `LoadLocation` works.

`cmd/today/main.go`:
```go
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"time"
	_ "time/tzdata"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/today"
)

const topic = "hockeytrack/games/today"

func run(ctx context.Context, scheduleURL string, store gamestore.Store, pub iotpub.Publisher) error {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, scheduleURL, nil)
	req.Header.Set("User-Agent", "hockeytrack-scoreboard/1.0 (+https://github.com/DavidJDrake/hockeytrack-scoreboard)")
	resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("schedule: status %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	active, err := store.ListActive(ctx)
	if err != nil {
		return err
	}
	states := map[int64]string{}
	for _, s := range active {
		states[s.GameID] = s.GameState
	}
	doc, err := today.Build(body, states, time.Now())
	if err != nil {
		return err
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	return pub.Publish(ctx, topic, out, true)
}

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	endpoint := os.Getenv("IOT_ENDPOINT")
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	store := gamestore.NewDynamo(dynamodb.NewFromConfig(cfg), os.Getenv("GAMES_TABLE"))
	url := os.Getenv("SCHEDULE_URL") // https://hockeytrack.davidjdrake.com/data/schedule.json
	lambda.Start(func(ctx context.Context) error { return run(ctx, url, store, iotpub.NewIoT(iot)) })
}
```
- [ ] **Step 4: Run, commit**

Run: `cd cloud && go vet ./... && go test ./...`
Expected: PASS.

```bash
git add cloud && git commit -m "today Lambda: publish the day's game list from the public schedule"
```

---

### Task 10: Terraform

**Files:**
- Create: `terraform/providers.tf`, `variables.tf`, `dynamodb.tf`, `lambda.tf`, `rule.tf`, `dlq.tf`, `iot.tf`, `iam.tf`, `scheduler.tf`, `outputs.tf`

**Interfaces:**
- Produces: outputs `iot_endpoint` (ATS data endpoint), `device_policy_name`, `games_table`. Resource names prefixed `scoreboard-`.

- [ ] **Step 1: Providers and state**

`providers.tf` (state in the existing HockeyTrack state bucket under its own key; S3-native locking):
```hcl
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.0" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
  }
  backend "s3" {
    bucket       = "hockeytrack-tfstate-989232581535"
    key          = "hockeytrack-scoreboard/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
```

`variables.tf`:
```hcl
variable "region" {
  type    = string
  default = "us-east-1"
}

variable "bus_name" {
  type    = string
  default = "hockeytrack"
}

variable "schedule_url" {
  type    = string
  default = "https://hockeytrack.davidjdrake.com/data/schedule.json"
}

variable "alerts_topic_name" {
  type    = string
  default = "hockeytrack-alerts"
}
```

- [ ] **Step 2: Table, Lambdas, rule, DLQ**

`dynamodb.tf`:
```hcl
resource "aws_dynamodb_table" "games" {
  name         = "scoreboard-games"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "gameId"
  attribute {
    name = "gameId"
    type = "N"
  }
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }
}
```

`lambda.tf`:
```hcl
data "aws_iot_endpoint" "data" {
  endpoint_type = "iot:Data-ATS"
}

data "archive_file" "reducer" {
  type        = "zip"
  source_file = "${path.module}/../build/reducer/bootstrap"
  output_path = "${path.module}/../build/reducer.zip"
}

data "archive_file" "today" {
  type        = "zip"
  source_file = "${path.module}/../build/today/bootstrap"
  output_path = "${path.module}/../build/today.zip"
}

resource "aws_lambda_function" "reducer" {
  function_name    = "scoreboard-reducer"
  role             = aws_iam_role.reducer.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.reducer.output_path
  source_code_hash = data.archive_file.reducer.output_base64sha256
  timeout          = 10
  memory_size      = 128
  environment {
    variables = {
      GAMES_TABLE  = aws_dynamodb_table.games.name
      IOT_ENDPOINT = "https://${data.aws_iot_endpoint.data.endpoint_address}"
    }
  }
  dead_letter_config {
    target_arn = aws_sqs_queue.dlq.arn
  }
}

resource "aws_lambda_function" "today" {
  function_name    = "scoreboard-today"
  role             = aws_iam_role.today.arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = data.archive_file.today.output_path
  source_code_hash = data.archive_file.today.output_base64sha256
  timeout          = 30
  memory_size      = 128
  environment {
    variables = {
      GAMES_TABLE  = aws_dynamodb_table.games.name
      IOT_ENDPOINT = "https://${data.aws_iot_endpoint.data.endpoint_address}"
      SCHEDULE_URL = var.schedule_url
    }
  }
}

resource "aws_cloudwatch_log_group" "reducer" {
  name              = "/aws/lambda/${aws_lambda_function.reducer.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "today" {
  name              = "/aws/lambda/${aws_lambda_function.today.function_name}"
  retention_in_days = 30
}
```

`rule.tf`:
```hcl
# The whole integration with HockeyTrack: one rule on its bus.
resource "aws_cloudwatch_event_rule" "game_events" {
  name           = "scoreboard-game-events"
  event_bus_name = var.bus_name
  event_pattern = jsonencode({
    source        = ["hockeytrack.poller"]
    "detail-type" = ["nhl.game.status", "nhl.game.clock", "nhl.game.play", "nhl.game.roster", "nhl.game.final"]
  })
}

resource "aws_cloudwatch_event_target" "reducer" {
  rule           = aws_cloudwatch_event_rule.game_events.name
  event_bus_name = var.bus_name
  arn            = aws_lambda_function.reducer.arn
  dead_letter_config {
    arn = aws_sqs_queue.dlq.arn
  }
  retry_policy {
    maximum_retry_attempts       = 4
    maximum_event_age_in_seconds = 300 # a scoreboard event older than 5 minutes is stale
  }
}

resource "aws_lambda_permission" "events_invoke_reducer" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reducer.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.game_events.arn
}
```

`dlq.tf`:
```hcl
resource "aws_sqs_queue" "dlq" {
  name                      = "scoreboard-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.dlq.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.game_events.arn } }
    }]
  })
}

data "aws_sns_topic" "alerts" {
  name = var.alerts_topic_name
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "scoreboard-dlq-depth"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [data.aws_sns_topic.alerts.arn]
}
```

- [ ] **Step 3: IAM, IoT policy, scheduler**

`iam.tf`:
```hcl
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  topic_arn_prefix = "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topic/hockeytrack/games"
  logs             = ["logs:CreateLogStream", "logs:PutLogEvents"]
}

resource "aws_iam_role" "reducer" {
  name               = "scoreboard-reducer"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "reducer" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.reducer.arn}:*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = [aws_dynamodb_table.games.arn]
  }
  statement {
    actions   = ["iot:Publish"]
    resources = ["${local.topic_arn_prefix}/*"]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.dlq.arn]
  }
}

resource "aws_iam_role_policy" "reducer" {
  role   = aws_iam_role.reducer.id
  policy = data.aws_iam_policy_document.reducer.json
}

resource "aws_iam_role" "today" {
  name               = "scoreboard-today"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

data "aws_iam_policy_document" "today" {
  statement {
    actions   = local.logs
    resources = ["${aws_cloudwatch_log_group.today.arn}:*"]
  }
  statement {
    actions   = ["dynamodb:Scan"]
    resources = [aws_dynamodb_table.games.arn]
  }
  statement {
    actions   = ["iot:Publish"]
    resources = ["${local.topic_arn_prefix}/today"]
  }
}

resource "aws_iam_role_policy" "today" {
  role   = aws_iam_role.today.id
  policy = data.aws_iam_policy_document.today.json
}
```

`iot.tf` (the device policy; things and certificates are created per device by `tools/provision.sh`):
```hcl
resource "aws_iot_policy" "device" {
  name = "scoreboard-device"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iot:Connect"]
        Resource = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:client/$${iot:Connection.Thing.ThingName}"]
      },
      {
        Effect   = "Allow"
        Action   = ["iot:Subscribe"]
        Resource = ["arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:topicfilter/hockeytrack/games/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["iot:Receive"]
        Resource = ["${local.topic_arn_prefix}/*"]
      }
    ]
  })
}
```
(`$${...}` is Terraform's escape for a literal `${iot:Connection.Thing.ThingName}` policy variable.)

`scheduler.tf` (note the trust condition uses the **schedule group** ARN):
```hcl
resource "aws_scheduler_schedule_group" "main" {
  name = "scoreboard"
}

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_scheduler_schedule_group.main.arn]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "scoreboard-scheduler-invoke"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.today.arn }]
  })
}

resource "aws_scheduler_schedule" "today" {
  name                = "scoreboard-today"
  group_name          = aws_scheduler_schedule_group.main.name
  schedule_expression = "rate(10 minutes)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.today.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}
```

`outputs.tf`:
```hcl
output "iot_endpoint" {
  value = data.aws_iot_endpoint.data.endpoint_address
}

output "device_policy_name" {
  value = aws_iot_policy.device.name
}

output "games_table" {
  value = aws_dynamodb_table.games.name
}
```

- [ ] **Step 4: Validate offline, then plan**

```bash
make build
export XDG_RUNTIME_DIR=$HOME/.cache/xdg-runtime && mkdir -p $XDG_RUNTIME_DIR
cd terraform && terraform init && terraform fmt -check && terraform validate && terraform plan
```
Expected: validate succeeds; plan shows ~20 resources to add and nothing to change or destroy in HockeyTrack's stack (this is a separate state file).

- [ ] **Step 5: Apply (orchestrator, in-session) and verify the first retained message**

```bash
cd terraform && terraform apply
aws scheduler get-schedule --name scoreboard-today --group-name scoreboard --region us-east-1 --query State
# Wait ≤10 minutes, then confirm the retained "today" document exists:
aws iot-data get-retained-message --topic hockeytrack/games/today --region us-east-1 --query payload --output text | base64 -d | head -c 400
```
Expected: `ENABLED`; a JSON document with `generatedAt` and today's games. If the schedule never fires, re-check the scheduler role's trust condition against the lesson in Global Constraints before anything else.

- [ ] **Step 6: Commit**

```bash
git add terraform && git commit -m "terraform: reducer + today Lambdas, bus rule with DLQ, table, IoT device policy, scheduler"
```

---

### Task 11: Device model

**Files:**
- Create: `device/scoreboard/model.py`, `device/tests/fixtures/state_live.json`, `device/tests/fixtures/state_pre.json`, `device/tests/fixtures/today.json`
- Test: `device/tests/test_model.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True) class Team: abbrev: str; score: int; sog: int; color: tuple[int, int, int]
@dataclass(frozen=True) class Penalty: team: str; number: int; seconds: int; type: str; ends_on_goal: bool
@dataclass(frozen=True) class GameState:  # parsed document
    game_id: int; state: str; as_of_ms: int; away: Team; home: Team
    period_number: int; period_type: str; period_label: str
    clock_seconds: int; clock_running: bool; intermission: bool
    situation_code: str; pp: str | None; empty_net: str | None
    penalties: tuple[Penalty, ...]; last_goal: tuple[str, int, int] | None; start: str | None
    @classmethod def from_json(cls, data: bytes | str) -> "GameState"
    def clock_at(self, now_ms: int) -> int            # seconds shown on the panel at wall time now_ms
    def penalties_at(self, now_ms: int) -> tuple[Penalty, ...]   # remaining seconds ticked down, expired dropped
    def goal_flash(self, now_ms: int) -> bool         # True for 3 s after last_goal.asOf
    def seconds_to_start(self, now_ms: int) -> int | None   # pre-game countdown
@dataclass(frozen=True) class TodayGame: game_id: int; away: str; home: str; start: str; state: str
def parse_today(data: bytes | str) -> list[TodayGame]
GameState.pregame(g: TodayGame) -> GameState        # a PRE document built from the day list (the reducer has no start time until a game is polled)
def fmt_clock(seconds: int) -> str                    # 872 → "14:32"; 59 → "0:59"; tenths not shown
```

- [ ] **Step 1: Fixtures**

`device/tests/fixtures/state_live.json` (the spec §3.2 example, `asOf` 1791135723123):
```json
{"v":1,"gameId":2026020001,"state":"LIVE","asOf":1791135723123,
 "away":{"abbrev":"TBL","score":2,"sog":17,"color":"002868"},
 "home":{"abbrev":"NYR","score":1,"sog":22,"color":"0038A8"},
 "period":{"number":2,"type":"REG","label":"2"},
 "clock":{"seconds":872,"running":true,"intermission":false},
 "situation":{"code":"1451","pp":"TBL"},
 "penalties":[{"team":"NYR","number":23,"seconds":74,"type":"MIN","endsOnGoal":true}],
 "lastGoal":{"team":"TBL","number":86,"asOf":1791135690000},
 "start":"2026-10-01T23:30:00Z"}
```
`state_pre.json`:
```json
{"v":1,"gameId":2026020001,"state":"PRE","asOf":1791100000000,
 "away":{"abbrev":"TBL","score":0,"sog":0,"color":"002868"},
 "home":{"abbrev":"NYR","score":0,"sog":0,"color":"0038A8"},
 "period":{"number":0,"type":"","label":""},
 "clock":{"seconds":0,"running":false,"intermission":false},
 "situation":{"code":""},"penalties":[],"start":"2026-10-01T23:30:00Z"}
```
`today.json`:
```json
{"generatedAt":1791100000000,"games":[
 {"gameId":2026020001,"away":"TBL","home":"NYR","start":"2026-10-01T23:30:00Z","state":"PRE"},
 {"gameId":2026020002,"away":"CHI","home":"FLA","start":"2026-10-02T00:00:00Z","state":"PRE"}]}
```

- [ ] **Step 2: Write the failing tests**

`device/tests/test_model.py`:
```python
from pathlib import Path

import pytest

from scoreboard.model import GameState, fmt_clock, parse_today

FIX = Path(__file__).parent / "fixtures"


def live() -> GameState:
    return GameState.from_json((FIX / "state_live.json").read_bytes())


def test_parse_live_document():
    s = live()
    assert s.game_id == 2026020001 and s.state == "LIVE"
    assert s.away.abbrev == "TBL" and s.away.color == (0x00, 0x28, 0x68)
    assert s.home.score == 1 and s.home.sog == 22
    assert s.period_label == "2" and s.clock_seconds == 872 and s.clock_running
    assert s.pp == "TBL" and s.empty_net is None
    assert len(s.penalties) == 1 and s.penalties[0].number == 23 and s.penalties[0].ends_on_goal
    assert s.last_goal == ("TBL", 86, 1791135690000)


def test_clock_counts_down_locally_while_running():
    s = live()
    assert s.clock_at(s.as_of_ms) == 872
    assert s.clock_at(s.as_of_ms + 10_000) == 862
    assert s.clock_at(s.as_of_ms + 10_400) == 862  # floor, never rounds up
    assert s.clock_at(s.as_of_ms + 900_000) == 0   # never negative
    assert s.clock_at(s.as_of_ms - 5_000) == 872   # clock skew: never ahead of the sample


def test_clock_holds_when_stopped():
    s = GameState.from_json((FIX / "state_live.json").read_text().replace('"running":true', '"running":false'))
    assert s.clock_at(s.as_of_ms + 60_000) == 872


def test_penalties_tick_and_expire():
    s = live()
    assert s.penalties_at(s.as_of_ms)[0].seconds == 74
    assert s.penalties_at(s.as_of_ms + 30_000)[0].seconds == 44
    assert s.penalties_at(s.as_of_ms + 80_000) == ()


def test_goal_flash_window():
    s = live()
    assert s.goal_flash(1791135690000 + 2_999)
    assert not s.goal_flash(1791135690000 + 3_001)


def test_pregame_countdown():
    s = GameState.from_json((FIX / "state_pre.json").read_bytes())
    # 2026-10-01T23:30:00Z is 1791165000000 ms since the epoch.
    assert s.seconds_to_start(1791165000000 - 90_000) == 90
    assert s.seconds_to_start(1791165000000 + 1) == 0
    assert live().seconds_to_start(0) is None  # not pre-game


def test_parse_today():
    games = parse_today((FIX / "today.json").read_bytes())
    assert [g.game_id for g in games] == [2026020001, 2026020002]
    assert games[0].away == "TBL" and games[0].state == "PRE"


def test_pregame_document_from_today_entry():
    g = parse_today((FIX / "today.json").read_bytes())[0]
    s = GameState.pregame(g)
    assert s.state == "PRE" and s.game_id == 2026020001 and s.away.abbrev == "TBL" and s.home.abbrev == "NYR"
    assert s.seconds_to_start(1791165000000 - 60_000) == 60
    assert s.penalties == () and s.last_goal is None


@pytest.mark.parametrize("secs,text", [(872, "14:32"), (59, "0:59"), (0, "0:00"), (1200, "20:00"), (3599, "59:59")])
def test_fmt_clock(secs, text):
    assert fmt_clock(secs) == text


def test_rejects_wrong_version():
    with pytest.raises(ValueError):
        GameState.from_json('{"v":2,"gameId":1}')
```

- [ ] **Step 3: Run to verify failure**

Run: `cd device && SDL_VIDEODRIVER=dummy .venv/bin/pytest tests/test_model.py -q`
Expected: ImportError (`scoreboard.model`).

- [ ] **Step 4: Implement `model.py`**

```python
"""The scoreboard state document, parsed, plus the time-derived views the
renderer needs (local clock, ticking penalties, goal flash, countdown)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

GOAL_FLASH_MS = 3000


def _hex(color: str) -> tuple[int, int, int]:
    try:
        return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
    except (ValueError, IndexError):
        return (136, 136, 136)


@dataclass(frozen=True)
class Team:
    abbrev: str
    score: int
    sog: int
    color: tuple[int, int, int]


@dataclass(frozen=True)
class Penalty:
    team: str
    number: int
    seconds: int
    type: str
    ends_on_goal: bool


@dataclass(frozen=True)
class GameState:
    game_id: int
    state: str
    as_of_ms: int
    away: Team
    home: Team
    period_number: int
    period_type: str
    period_label: str
    clock_seconds: int
    clock_running: bool
    intermission: bool
    situation_code: str
    pp: str | None
    empty_net: str | None
    penalties: tuple[Penalty, ...]
    last_goal: tuple[str, int, int] | None
    start: str | None

    @classmethod
    def from_json(cls, data: bytes | str) -> "GameState":
        d = json.loads(data)
        if d.get("v") != 1:
            raise ValueError(f"unsupported state document version {d.get('v')!r}")
        team = lambda t: Team(str(t.get("abbrev", "")), int(t.get("score", 0)), int(t.get("sog", 0)), _hex(str(t.get("color", ""))))
        period = d.get("period", {})
        clock = d.get("clock", {})
        sit = d.get("situation", {})
        goal = d.get("lastGoal")
        return cls(
            game_id=int(d["gameId"]),
            state=str(d.get("state", "PRE")),
            as_of_ms=int(d.get("asOf", 0)),
            away=team(d.get("away", {})),
            home=team(d.get("home", {})),
            period_number=int(period.get("number", 0)),
            period_type=str(period.get("type", "")),
            period_label=str(period.get("label", "")),
            clock_seconds=int(clock.get("seconds", 0)),
            clock_running=bool(clock.get("running", False)),
            intermission=bool(clock.get("intermission", False)),
            situation_code=str(sit.get("code", "")),
            pp=sit.get("pp") or None,
            empty_net=sit.get("emptyNet") or None,
            penalties=tuple(
                Penalty(str(p.get("team", "")), int(p.get("number", 0)), int(p.get("seconds", 0)), str(p.get("type", "")), bool(p.get("endsOnGoal", False)))
                for p in d.get("penalties") or []
            ),
            last_goal=(str(goal["team"]), int(goal.get("number", 0)), int(goal.get("asOf", 0))) if goal else None,
            start=d.get("start") or None,
        )

    def _elapsed_s(self, now_ms: int) -> int:
        return max(0, (now_ms - self.as_of_ms) // 1000)

    def clock_at(self, now_ms: int) -> int:
        if not self.clock_running:
            return self.clock_seconds
        return max(0, self.clock_seconds - self._elapsed_s(now_ms))

    def penalties_at(self, now_ms: int) -> tuple[Penalty, ...]:
        if not self.clock_running:
            return self.penalties
        e = self._elapsed_s(now_ms)
        out = []
        for p in self.penalties:
            left = p.seconds - e
            if left > 0:
                out.append(Penalty(p.team, p.number, left, p.type, p.ends_on_goal))
        return tuple(out)

    def goal_flash(self, now_ms: int) -> bool:
        return self.last_goal is not None and 0 <= now_ms - self.last_goal[2] <= GOAL_FLASH_MS

    def seconds_to_start(self, now_ms: int) -> int | None:
        if self.state != "PRE" or not self.start:
            return None
        start_ms = int(datetime.fromisoformat(self.start.replace("Z", "+00:00")).timestamp() * 1000)
        return max(0, (start_ms - now_ms) // 1000)

    @classmethod
    def pregame(cls, g: "TodayGame") -> "GameState":
        """A PRE document for a game the reducer has not seen yet, so the
        panel can count down before the poller's first event."""
        grey = (136, 136, 136)
        return cls(game_id=g.game_id, state="PRE", as_of_ms=0, away=Team(g.away, 0, 0, grey), home=Team(g.home, 0, 0, grey),
                   period_number=0, period_type="", period_label="", clock_seconds=0, clock_running=False, intermission=False,
                   situation_code="", pp=None, empty_net=None, penalties=(), last_goal=None, start=g.start or None)


@dataclass(frozen=True)
class TodayGame:
    game_id: int
    away: str
    home: str
    start: str
    state: str


def parse_today(data: bytes | str) -> list[TodayGame]:
    d = json.loads(data)
    return [TodayGame(int(g["gameId"]), str(g.get("away", "")), str(g.get("home", "")), str(g.get("start", "")), str(g.get("state", "PRE"))) for g in d.get("games", [])]


def fmt_clock(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
```

- [ ] **Step 5: Run tests, commit**

Run: `cd device && SDL_VIDEODRIVER=dummy .venv/bin/pytest -q`
Expected: all pass.

```bash
git add device && git commit -m "device: state document model with local clock, penalty ticking, goal flash, countdown"
```

---

### Task 12: Renderer

**Files:**
- Create: `device/scoreboard/assets.py`, `device/scoreboard/render.py`, `device/scoreboard/fonts/BarlowCondensed-Bold.ttf`, `device/scoreboard/fonts/BarlowCondensed-SemiBold.ttf`, `device/scoreboard/fonts/OFL.txt`
- Test: `device/tests/test_render.py`

**Interfaces:**
- Produces:
```python
class Assets:  # loaded once
    def __init__(self, root: Path | None = None) -> None
    def font(self, px: int, bold: bool = True) -> pygame.font.Font   # cached
    def logo(self, abbrev: str) -> pygame.Surface | None            # from scoreboard/logos/<ABBREV>.png if present
W, H = 1920, 480
def draw(surface: pygame.Surface, state: GameState | None, now_ms: int, assets: Assets, link_ok: bool = True) -> None
```
Layout per spec §3.5; `state is None` draws a "waiting for HockeyTrack…" screen.

- [ ] **Step 1: Fonts**

Download Barlow Condensed (SIL Open Font License) from Google Fonts and keep only the two weights plus the licence:

```bash
cd device/scoreboard && mkdir -p fonts && cd fonts
curl -sL "https://github.com/googlefonts/barlow/raw/main/fonts/ttf/BarlowCondensed-Bold.ttf" -o BarlowCondensed-Bold.ttf
curl -sL "https://github.com/googlefonts/barlow/raw/main/fonts/ttf/BarlowCondensed-SemiBold.ttf" -o BarlowCondensed-SemiBold.ttf
curl -sL "https://github.com/googlefonts/barlow/raw/main/OFL.txt" -o OFL.txt
file BarlowCondensed-Bold.ttf   # expect: TrueType Font data
```
If those paths 404, download the family zip from https://fonts.google.com/specimen/Barlow+Condensed and copy the two files. The HockeyTrack site ships the same face as woff2; TTF is needed here because pygame's font loader does not read woff2.

- [ ] **Step 2: Write the failing test**

`device/tests/test_render.py`:
```python
import os
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from scoreboard.assets import Assets  # noqa: E402
from scoreboard.model import GameState  # noqa: E402
from scoreboard.render import H, W, draw  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def surface():
    pygame.init()
    return pygame.Surface((W, H))


def test_draw_live_frame_paints_team_colours_and_clock():
    surf, assets = surface(), Assets()
    s = GameState.from_json((FIX / "state_live.json").read_bytes())
    draw(surf, s, s.as_of_ms, assets)
    # Away colour appears on the left third, home colour on the right third.
    left = {surf.get_at((x, y))[:3] for x in range(20, 600, 10) for y in range(20, 460, 10)}
    right = {surf.get_at((x, y))[:3] for x in range(1320, 1900, 10) for y in range(20, 460, 10)}
    assert (0x00, 0x28, 0x68) in left, "TBL colour missing from the away side"
    assert (0x00, 0x38, 0xA8) in right, "NYR colour missing from the home side"
    # Something bright is drawn in the centre band where the clock lives.
    centre = [surf.get_at((x, y))[:3] for x in range(760, 1160, 4) for y in range(120, 300, 4)]
    assert any(max(c) > 200 for c in centre), "clock digits not drawn"


def test_draw_handles_every_state_without_error():
    surf, assets = surface(), Assets()
    for name in ("state_live.json", "state_pre.json"):
        s = GameState.from_json((FIX / name).read_bytes())
        for offset in (0, 60_000, 3_600_000):
            draw(surf, s, s.as_of_ms + offset, assets)
    draw(surf, None, 0, assets)
    draw(surf, GameState.from_json((FIX / "state_live.json").read_text().replace('"state":"LIVE"', '"state":"FINAL"')), 0, assets, link_ok=False)


def test_penalty_row_shrinks_as_time_passes():
    surf, assets = surface(), Assets()
    s = GameState.from_json((FIX / "state_live.json").read_bytes())
    def bar_width(t):
        draw(surf, s, t, assets)
        row = [x for x in range(0, W) if surf.get_at((x, 426))[:3] == (0x00, 0x38, 0xA8)]  # the bar sits at y 422–430
        return len(row)
    assert bar_width(s.as_of_ms) > bar_width(s.as_of_ms + 40_000) > 0
```

- [ ] **Step 3: Run to verify failure**

Run: `cd device && SDL_VIDEODRIVER=dummy .venv/bin/pytest tests/test_render.py -q`
Expected: ImportError.

- [ ] **Step 4: Implement `assets.py` and `render.py`**

`assets.py`:
```python
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pygame

HERE = Path(__file__).parent


class Assets:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or HERE
        pygame.font.init()
        self._logos: dict[str, pygame.Surface | None] = {}

    @lru_cache(maxsize=64)
    def font(self, px: int, bold: bool = True) -> pygame.font.Font:
        name = "BarlowCondensed-Bold.ttf" if bold else "BarlowCondensed-SemiBold.ttf"
        path = self.root / "fonts" / name
        if path.exists():
            return pygame.font.Font(str(path), px)
        return pygame.font.SysFont("dejavusanscondensed,sans", px, bold=bold)

    def logo(self, abbrev: str) -> pygame.Surface | None:
        if abbrev not in self._logos:
            path = self.root / "logos" / f"{abbrev}.png"
            self._logos[abbrev] = pygame.image.load(str(path)).convert_alpha() if path.exists() else None
        return self._logos[abbrev]
```

`render.py`:
```python
"""Paint one frame of the scoreboard onto a 1920x480 surface."""
from __future__ import annotations

import pygame

from .assets import Assets
from .model import GameState, fmt_clock

W, H = 1920, 480
INK = (250, 250, 250)
MUTED = (150, 158, 168)
BG = (10, 10, 12)
RED = (200, 16, 46)
RULE = (48, 52, 60)


def _text(surface, assets, s, px, color, x, y, anchor="topleft", bold=True):
    img = assets.font(px, bold).render(s, True, color)
    rect = img.get_rect(**{anchor: (x, y)})
    surface.blit(img, rect)
    return rect


def _side(surface, assets, team, x_abbrev, x_score, align, pp_here, en_here, flash):
    """One team's column. align is 'left' (away) or 'right' (home)."""
    if flash:
        pygame.draw.rect(surface, team.color, (0 if align == "left" else W // 2, 0, W // 2, H))
        fg = INK
    else:
        fg = team.color
    anchor = "topleft" if align == "left" else "topright"
    _text(surface, assets, team.abbrev, 150, fg if not flash else INK, x_abbrev, 40, anchor)
    _text(surface, assets, str(team.score), 190, INK, x_score, 20, anchor)
    label = "EN" if en_here else f"SOG {team.sog}"
    _text(surface, assets, label, 60, MUTED if not flash else INK, x_abbrev, 205, anchor, bold=False)
    if pp_here:
        _text(surface, assets, "POWER PLAY", 44, RED if not flash else INK, x_abbrev, 270, anchor)


def _penalty_rows(surface, assets, state, now_ms, y):
    pens = state.penalties_at(now_ms)
    for side in ("away", "home"):
        team = state.away if side == "away" else state.home
        rows = [p for p in pens if p.team == team.abbrev][:2]
        for i, p in enumerate(rows):
            ry = y + i * 52
            x0 = 60 if side == "away" else W // 2 + 60
            width = 720
            frac = p.seconds / max(1, 120 if p.type in ("MIN", "BEN") else 300 if p.type == "MAJ" else 600)
            pygame.draw.rect(surface, RULE, (x0, ry + 36, width, 8))
            pygame.draw.rect(surface, team.color, (x0, ry + 36, int(width * min(1.0, frac)), 8))
            _text(surface, assets, f"#{p.number}  {p.team}  {fmt_clock(p.seconds)}", 40, INK, x0, ry - 6, "topleft", bold=False)


def draw(surface: pygame.Surface, state: GameState | None, now_ms: int, assets: Assets, link_ok: bool = True) -> None:
    surface.fill(BG)
    if state is None:
        _text(surface, assets, "HOCKEYTRACK", 120, INK, W // 2, H // 2 - 40, "center")
        _text(surface, assets, "waiting for a game…", 48, MUTED, W // 2, H // 2 + 60, "center", bold=False)
        return

    if state.state == "PRE":
        left = state.seconds_to_start(now_ms) or 0
        d, rem = divmod(left, 86400)
        h, rem = divmod(rem, 3600)
        m, s = divmod(rem, 60)
        _text(surface, assets, f"{state.away.abbrev} @ {state.home.abbrev}", 110, INK, W // 2, 90, "center")
        _text(surface, assets, "PUCK DROP", 44, RED, W // 2, 175, "center")
        _text(surface, assets, f"{d}d {h:02d}:{m:02d}:{s:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}", 200, RED, W // 2, 300, "center")
        return

    flash_team = state.last_goal[0] if state.goal_flash(now_ms) else None
    _side(surface, assets, state.away, 60, 620, "left", state.pp == state.away.abbrev, state.empty_net == state.away.abbrev, flash_team == state.away.abbrev)
    _side(surface, assets, state.home, W - 60, W - 620, "right", state.pp == state.home.abbrev, state.empty_net == state.home.abbrev, flash_team == state.home.abbrev)

    # Centre: clock and period.
    if state.state == "FINAL":
        _text(surface, assets, "FINAL", 200, INK, W // 2, 150, "center")
        if state.period_type in ("OT", "SO"):
            _text(surface, assets, state.period_label, 60, MUTED, W // 2, 290, "center")
    elif state.intermission:
        _text(surface, assets, "INTERMISSION", 70, MUTED, W // 2, 110, "center")
        _text(surface, assets, fmt_clock(state.clock_at(now_ms)), 170, INK, W // 2, 220, "center")
    else:
        _text(surface, assets, fmt_clock(state.clock_at(now_ms)), 220, INK, W // 2, 150, "center")
        suffix = "PERIOD" if state.period_label.isdigit() else ""
        label = {"1": "1ST", "2": "2ND", "3": "3RD"}.get(state.period_label, state.period_label)
        _text(surface, assets, f"{label} {suffix}".strip(), 60, MUTED, W // 2, 300, "center")

    if flash_team and state.last_goal:
        _text(surface, assets, f"GOAL  #{state.last_goal[1]}", 90, INK, W // 2, 400, "center")
    else:
        pygame.draw.line(surface, RULE, (60, 372), (W - 60, 372), 2)
        _penalty_rows(surface, assets, state, now_ms, 386)

    if not link_ok:
        pygame.draw.circle(surface, RED, (W - 24, 24), 8)
```

- [ ] **Step 5: Run the tests; then look at a frame**

Run: `cd device && SDL_VIDEODRIVER=dummy .venv/bin/pytest -q`
Expected: pass. Then render one PNG to eyeball:

```bash
cd device && SDL_VIDEODRIVER=dummy .venv/bin/python -c "
import pygame; from pathlib import Path
from scoreboard.assets import Assets; from scoreboard.model import GameState; from scoreboard.render import W,H,draw
pygame.init(); s=pygame.Surface((W,H)); st=GameState.from_json(Path('tests/fixtures/state_live.json').read_bytes())
draw(s, st, st.as_of_ms+5000, Assets()); pygame.image.save(s, '/tmp/frame.png'); print('ok')"
```
Open `/tmp/frame.png` (Read tool) and adjust sizes/positions in `render.py` until the layout matches spec §3.5: nothing clipped, clock centred, penalty rows along the bottom. Re-run the tests after any change.

- [ ] **Step 6: Commit**

```bash
git add device && git commit -m "device: 1920x480 renderer with fonts, penalty bars, goal flash, pre-game and final screens"
```

---

### Task 13: MQTT link, buttons, main loop, systemd unit

**Files:**
- Create: `device/scoreboard/link.py`, `device/scoreboard/buttons.py`, `device/scoreboard/main.py`, `device/scoreboard/config.py`, `device/scoreboard.service`
- Test: `device/tests/test_link.py`

**Interfaces:**
- Produces:
```python
class Link:  # scoreboard/link.py
    def __init__(self, endpoint: str, client_id: str, cert: Path, key: Path, ca: Path, on_state, on_today, on_link) -> None
    def start(self) -> None; def stop(self) -> None
    def follow(self, game_id: int | None) -> None   # (un)subscribe the state topic
    @staticmethod def route(topic: str, payload: bytes, on_state, on_today) -> None   # pure dispatch, tested
class Config:  # scoreboard/config.py — reads device/config/device.json + file paths
    endpoint: str; client_id: str; cert: Path; key: Path; ca: Path; state_file: Path; brightness: float
    def load_game_id(self) -> int | None; def save_game_id(self, game_id: int | None) -> None
```
- `main.py`: loop at 10 Hz: `link` callbacks push into a `queue.Queue`; the loop drains it, updates `current: GameState | None` and `today: list[TodayGame]`, calls `draw`, flips the display. Buttons: `on_a` cycles `today` (wrapping; None = follow nothing), `on_b` cycles brightness 1.0 → 0.6 → 0.3. Screen blanks after 30 minutes with no game selected.

- [ ] **Step 1: Write the failing test for the pure routing**

`device/tests/test_link.py`:
```python
from scoreboard.link import Link


def test_route_dispatches_by_topic():
    got = {}
    Link.route("hockeytrack/games/2026020001/state", b'{"v":1,"gameId":2026020001}', on_state=lambda gid, b: got.setdefault("state", (gid, b)), on_today=lambda b: got.setdefault("today", b))
    Link.route("hockeytrack/games/today", b'{"games":[]}', on_state=lambda gid, b: got.setdefault("state", (gid, b)), on_today=lambda b: got.setdefault("today", b))
    Link.route("hockeytrack/other", b"x", on_state=lambda *a: got.setdefault("bad", a), on_today=lambda *a: got.setdefault("bad", a))
    assert got["state"] == (2026020001, b'{"v":1,"gameId":2026020001}')
    assert got["today"] == b'{"games":[]}'
    assert "bad" not in got
```

- [ ] **Step 2: Run to verify failure**

Run: `cd device && SDL_VIDEODRIVER=dummy .venv/bin/pytest tests/test_link.py -q`
Expected: ImportError.

- [ ] **Step 3: Implement**

`config.py`:
```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # device/
CONFIG_DIR = ROOT / "config"


@dataclass
class Config:
    endpoint: str
    client_id: str
    cert: Path
    key: Path
    ca: Path
    state_file: Path
    brightness: float = 1.0

    @classmethod
    def load(cls, config_dir: Path = CONFIG_DIR) -> "Config":
        d = json.loads((config_dir / "device.json").read_text())
        return cls(
            endpoint=d["endpoint"], client_id=d["thingName"],
            cert=config_dir / "device.pem.crt", key=config_dir / "private.pem.key", ca=config_dir / "AmazonRootCA1.pem",
            state_file=config_dir / "state.json", brightness=float(d.get("brightness", 1.0)),
        )

    def load_game_id(self) -> int | None:
        try:
            return json.loads(self.state_file.read_text()).get("gameId")
        except (OSError, ValueError):
            return None

    def save_game_id(self, game_id: int | None) -> None:
        self.state_file.write_text(json.dumps({"gameId": game_id}))
```

`link.py`:
```python
"""MQTT connection to AWS IoT Core: TLS with the device certificate,
auto-reconnect, and routing of the two topics to callbacks."""
from __future__ import annotations

import logging
import ssl
from pathlib import Path

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)
TODAY = "hockeytrack/games/today"


class Link:
    def __init__(self, endpoint: str, client_id: str, cert: Path, key: Path, ca: Path, on_state, on_today, on_link) -> None:
        self.on_state, self.on_today, self.on_link = on_state, on_today, on_link
        self._game: int | None = None
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, protocol=mqtt.MQTTv311)
        self._client.tls_set(ca_certs=str(ca), certfile=str(cert), keyfile=str(key), tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._endpoint = endpoint

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._client.connect_async(self._endpoint, 8883, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def follow(self, game_id: int | None) -> None:
        if self._game is not None and self._game != game_id:
            self._client.unsubscribe(self._state_topic(self._game))
        self._game = game_id
        if game_id is not None and self._client.is_connected():
            self._client.subscribe(self._state_topic(game_id), qos=1)

    # --- callbacks ----------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("connected: %s", reason_code)
        client.subscribe(TODAY, qos=1)
        if self._game is not None:
            client.subscribe(self._state_topic(self._game), qos=1)
        self.on_link(True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning("disconnected: %s", reason_code)
        self.on_link(False)

    def _on_message(self, client, userdata, msg):
        self.route(msg.topic, msg.payload, self.on_state, self.on_today)

    @staticmethod
    def route(topic: str, payload: bytes, on_state, on_today) -> None:
        if topic == TODAY:
            on_today(payload)
            return
        parts = topic.split("/")
        if len(parts) == 4 and parts[:2] == ["hockeytrack", "games"] and parts[3] == "state" and parts[2].isdigit():
            on_state(int(parts[2]), payload)

    @staticmethod
    def _state_topic(game_id: int) -> str:
        return f"hockeytrack/games/{game_id}/state"
```

`buttons.py`:
```python
"""Optional GPIO buttons (BCM 5 = A: next game, BCM 6 = B: brightness).
Silently a no-op where gpiozero or the hardware is absent."""
from __future__ import annotations


def attach(on_a, on_b) -> bool:
    try:
        from gpiozero import Button  # type: ignore
    except Exception:
        return False
    try:
        a, b = Button(5, pull_up=True, bounce_time=0.05), Button(6, pull_up=True, bounce_time=0.05)
    except Exception:
        return False
    a.when_pressed, b.when_pressed = on_a, on_b
    attach._keep = (a, b)  # keep references alive
    return True
```

`main.py`:
```python
"""Service entry point: MQTT in, frames out, 10 Hz."""
from __future__ import annotations

import logging
import os
import queue
import time

import pygame

from . import buttons
from .assets import Assets
from .config import Config
from .link import Link
from .model import GameState, parse_today
from .render import H, W, draw

log = logging.getLogger("scoreboard")
BLANK_AFTER_S = 30 * 60


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))
    fixture = os.environ.get("SCOREBOARD_FIXTURE")  # desktop preview: render a fixture, no broker
    cfg = None if fixture else Config.load()
    events: queue.Queue = queue.Queue()
    link = None
    if cfg:
        link = Link(cfg.endpoint, cfg.client_id, cfg.cert, cfg.key, cfg.ca,
                    on_state=lambda gid, b: events.put(("state", gid, b)),
                    on_today=lambda b: events.put(("today", b)),
                    on_link=lambda ok: events.put(("link", ok)))
    pygame.init()
    pygame.mouse.set_visible(False)
    screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN if os.environ.get("DISPLAY") is None else 0)
    assets = Assets()
    frame = pygame.Surface((W, H))

    current: GameState | None = None
    today = []
    following = cfg.load_game_id() if cfg else None
    link_ok = bool(fixture)
    brightness = cfg.brightness if cfg else 1.0
    last_activity = time.time()
    if fixture:
        with open(fixture, "rb") as f:
            current = GameState.from_json(f.read())
        following = current.game_id

    def select(game_id):
        nonlocal following, current, last_activity
        following, current, last_activity = game_id, None, time.time()
        if cfg:
            cfg.save_game_id(game_id)
        if link:
            link.follow(game_id)
        pregame_from_today()
        log.info("following %s", game_id)

    def pregame_from_today():
        # Until the reducer has seen the game, show a countdown from the day list.
        nonlocal current
        if current is None or (current.state == "PRE" and not current.start):
            for g in today:
                if g.game_id == following:
                    current = GameState.pregame(g)

    def on_a():
        ids = [g.game_id for g in today]
        if not ids:
            return
        nxt = ids[(ids.index(following) + 1) % len(ids)] if following in ids else ids[0]
        events.put(("select", nxt))

    def on_b():
        events.put(("brightness", None))

    buttons.attach(on_a, on_b)
    if link:
        link.follow(following)
        link.start()
    clock = pygame.time.Clock()
    try:
        while True:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                    return
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_a:
                    on_a()
                if ev.type == pygame.KEYDOWN and ev.key == pygame.K_b:
                    on_b()
            while True:
                try:
                    item = events.get_nowait()
                except queue.Empty:
                    break
                kind = item[0]
                if kind == "state" and item[1] == following:
                    try:
                        current = GameState.from_json(item[2])
                    except ValueError as e:
                        log.warning("bad state doc: %s", e)
                elif kind == "today":
                    today = parse_today(item[1])
                    pregame_from_today()
                elif kind == "link":
                    link_ok = item[1]
                elif kind == "select":
                    select(item[1])
                elif kind == "brightness":
                    brightness = {1.0: 0.6, 0.6: 0.3}.get(brightness, 1.0)
            now_ms = int(time.time() * 1000)
            if following is None and time.time() - last_activity > BLANK_AFTER_S:
                frame.fill((0, 0, 0))
            else:
                draw(frame, current, now_ms, assets, link_ok)
            if brightness < 1.0:
                dim = pygame.Surface((W, H))
                dim.fill((0, 0, 0))
                dim.set_alpha(int(255 * (1 - brightness)))
                frame.blit(dim, (0, 0))
            screen.blit(frame, (0, 0))
            pygame.display.flip()
            clock.tick(10)
    finally:
        if link:
            link.stop()


if __name__ == "__main__":
    main()
```

`scoreboard.service`:
```ini
[Unit]
Description=HockeyTrack scoreboard
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/hockeytrack-scoreboard/device
Environment=SDL_VIDEODRIVER=kmsdrm
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/pi/hockeytrack-scoreboard/device/.venv/bin/python -m scoreboard.main
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Run tests; desktop smoke run**

Run: `cd device && SDL_VIDEODRIVER=dummy .venv/bin/pytest -q`
Expected: pass.

Desktop smoke (no broker, no config needed): `cd device && SCOREBOARD_FIXTURE=tests/fixtures/state_live.json .venv/bin/python -m scoreboard.main` opens a 1920×480 window with the clock ticking from the fixture; Esc quits, A/B keys stand in for the buttons.

- [ ] **Step 5: Commit**

```bash
git add device && git commit -m "device: MQTT link, buttons, main loop, systemd unit"
```

---

### Task 14: Provisioning script, README, end-to-end

**Files:**
- Create: `tools/provision.sh`
- Modify: `README.md`

- [ ] **Step 1: Provisioning script**

`tools/provision.sh`:
```bash
#!/usr/bin/env bash
# Create an IoT thing + certificate for one scoreboard and write the files
# the device needs into device/config/. Run once per device.
set -euo pipefail
NAME=${1:?usage: provision.sh <thing-name>}
REGION=${REGION:-us-east-1}
POLICY=${POLICY:-scoreboard-device}
OUT=$(cd "$(dirname "$0")/.." && pwd)/device/config
mkdir -p "$OUT"

aws iot create-thing --thing-name "$NAME" --region "$REGION" >/dev/null
CERT_JSON=$(aws iot create-keys-and-certificate --set-as-active --region "$REGION" \
  --certificate-pem-outfile "$OUT/device.pem.crt" --private-key-outfile "$OUT/private.pem.key" --public-key-outfile "$OUT/public.pem.key")
CERT_ARN=$(echo "$CERT_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["certificateArn"])')
aws iot attach-policy --policy-name "$POLICY" --target "$CERT_ARN" --region "$REGION"
aws iot attach-thing-principal --thing-name "$NAME" --principal "$CERT_ARN" --region "$REGION"
curl -sS https://www.amazontrust.com/repository/AmazonRootCA1.pem -o "$OUT/AmazonRootCA1.pem"
ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$REGION" --query endpointAddress --output text)
printf '{"thingName":"%s","endpoint":"%s","brightness":1.0}\n' "$NAME" "$ENDPOINT" > "$OUT/device.json"
chmod 600 "$OUT/private.pem.key"
echo "Provisioned $NAME. Copy device/config/ to the Pi at ~/hockeytrack-scoreboard/device/config/ (never commit it)."
```
`chmod +x tools/provision.sh`.

- [ ] **Step 2: README**

Replace the skeleton with: what it is (two sentences), a photo placeholder, the architecture diagram from the spec (§3), a parts list (spec §2 table), **Setup** in three numbered blocks — *Cloud* (`make deploy`, needs HockeyTrack's bus in the same account), *Device* (`make provision DEVICE=living-room`, flash Raspberry Pi OS Lite 64-bit, `sudo apt install python3-pygame libsdl2-2.0-0`, clone, `python3 -m venv --system-site-packages .venv && .venv/bin/pip install -r requirements.txt`, copy `device/config/`, `sudo cp device/scoreboard.service /etc/systemd/system/ && sudo systemctl enable --now scoreboard`), *Choosing a game* (buttons or edit `config/state.json`) — then **How it works** (state document example from spec §3.2 and the "one rule" point with the `rule.tf` pattern shown), **Development** (`make test`, the desktop fixture run from Task 13), and **Credits/Licence** (MIT; Barlow Condensed under OFL; NHL data via HockeyTrack; not affiliated with the NHL).

- [ ] **Step 3: End-to-end on the bench with fixture events**

No live games exist before 2026-09-19, so drive the real bus with the reducer's own fixture events. Create `tools/bus_fixture.sh`:

```bash
#!/usr/bin/env bash
# Put the reducer's fixture events onto the real hockeytrack bus, a few
# seconds apart, so a bench device can be watched end to end. Uses game
# 2025020001 (CHI @ FLA), which no real poller will touch.
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
```

`chmod +x tools/bus_fixture.sh`. On the bench: set the device to follow 2025020001 (`echo '{"gameId":2025020001}' > device/config/state.json`), start the service, run the script, and watch the panel move through PRE → penalty → goal flash → 2nd period 14:32 with FLA on the PP → FINAL. Each step should reach the panel within about a second of the `put`; confirm on the cloud side with:

```bash
aws iot-data get-retained-message --topic hockeytrack/games/2025020001/state --region us-east-1 --query payload --output text | base64 -d
```

On the first preseason game (HOC-33), repeat the retained-message check for the real game id: it should update every ~5 s while the game is live.

- [ ] **Step 4: Commit and (with the user's go-ahead) publish**

```bash
git add tools README.md && git commit -m "provisioning script and README; the worked example of consuming HockeyTrack"
```
Creating the public GitHub repository is the user's call: ask, then `gh repo create DavidJDrake/hockeytrack-scoreboard --public --source=. --push`.
