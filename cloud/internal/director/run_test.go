package director

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"hockeytrack-scoreboard/internal/accounts"
	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/panelconfig"
	"hockeytrack-scoreboard/internal/reduce"
	"hockeytrack-scoreboard/internal/schedule"
	"hockeytrack-scoreboard/internal/season"
	"hockeytrack-scoreboard/internal/settings"
)

// The season the run tests share: early and late tonight (they do not
// overlap: 7 PM and 10 PM Eastern), one tomorrow, and a stranger the same
// night that no panel keeps.
func testSeason(t *testing.T) season.Season {
	t.Helper()
	row := func(id int64, at time.Time) string {
		s := at.UTC().Format(time.RFC3339)
		return fmt.Sprintf(`{"id":%d,"date":"%s","start":"%s","away":"MTL","home":"TOR","type":2}`, id, s[:10], s)
	}
	sn, dropped, err := season.Parse([]byte(`{"teams":{"MTL":"Montréal Canadiens","TOR":"Toronto Maple Leafs"},"games":[` +
		strings.Join([]string{
			row(early, clock.Add(-30*time.Minute)),
			row(late, clock.Add(150*time.Minute)),
			row(tomorrow, clock.Add(24*time.Hour)),
			row(stranger, clock.Add(-time.Hour)),
		}, ",") + `]}`))
	if err != nil || dropped != 0 {
		t.Fatal(err, dropped)
	}
	return sn
}

type harness struct {
	*Director
	devices *devices.Fake
	games   *gamestore.Fake
	pub     *iotpub.Fake
	clock   time.Time
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	h := &harness{devices: devices.NewFake(), games: gamestore.NewFake(), pub: &iotpub.Fake{}, clock: clock}
	sn := testSeason(t)
	acct := accounts.NewFake()
	lead := 45
	_ = acct.SetDefaults(context.Background(), "sub-a", settings.Settings{CountdownLeadMin: &lead})
	h.Director = &Director{
		Devices:  h.devices,
		Games:    h.games,
		Accounts: acct,
		Season:   func(context.Context) (season.Season, error) { return sn, nil },
		Pub:      h.pub,
		Now:      func() time.Time { return h.clock },
	}
	return h
}

func (h *harness) panel(t *testing.T, thing, owner string, games ...int64) {
	t.Helper()
	ctx := context.Background()
	if err := h.devices.Register(ctx, thing); err != nil {
		t.Fatal(err)
	}
	if owner == "" {
		return
	}
	if err := h.devices.Claim(ctx, thing, owner); err != nil {
		t.Fatal(err)
	}
	if err := h.devices.Update(ctx, devices.Device{ThingName: thing, Owner: owner, Schedule: schedule.Panel{Games: games}}); err != nil {
		t.Fatal(err)
	}
}

func (h *harness) game(id int64, state string, finalAt time.Time) {
	s := reduce.State{GameID: id, GameState: state}
	if cur, ok, _ := h.games.Get(context.Background(), id); ok {
		s.Version = cur.Version
	}
	if state == "FINAL" {
		s.FinalAt = finalAt.UnixMilli()
	}
	_ = h.games.Put(context.Background(), s)
}

func (h *harness) row(t *testing.T, thing string) devices.Device {
	t.Helper()
	d, ok, err := h.devices.Get(context.Background(), thing)
	if err != nil || !ok {
		t.Fatal(thing, ok, err)
	}
	return d
}

type doc struct {
	GameID   *int64 `json:"gameId"`
	ChosenAt int64  `json:"chosenAt"`
	Display  struct {
		CountdownLeadMin int `json:"countdownLeadMin"`
		FinalHoldMin     int `json:"finalHoldMin"`
	} `json:"display"`
}

func decode(t *testing.T, m iotpub.Message) doc {
	t.Helper()
	var d doc
	if err := json.Unmarshal(m.Payload, &d); err != nil {
		t.Fatal(err)
	}
	return d
}

func TestTheNextKeptGameIsSentRetainedAsTheWholeDocumentAndRecorded(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early, late, tomorrow)
	if err := h.Run(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if len(h.pub.Messages) != 1 {
		t.Fatalf("published %d messages, want 1", len(h.pub.Messages))
	}
	m := h.pub.Messages[0]
	if m.Topic != panelconfig.Topic("scoreboard-01") || !m.Retain {
		t.Errorf("topic %q retain %v", m.Topic, m.Retain)
	}
	d := decode(t, m)
	// The document is the API's: the game, a fresh stamp, and the settings
	// with the account's defaults laid under the panel's.
	if d.GameID == nil || *d.GameID != early || d.ChosenAt != clock.UnixMilli() {
		t.Errorf("document %s", m.Payload)
	}
	if d.Display.CountdownLeadMin != 45 || d.Display.FinalHoldMin != settings.BuiltIn.FinalHoldMin {
		t.Errorf("settings %+v: the account's defaults were not laid under", d.Display)
	}
	row := h.row(t, "scoreboard-01")
	if row.GameID != early || row.Sent != early || row.ChosenAt != clock.UnixMilli() {
		t.Errorf("row %+v", row)
	}
	if fmt.Sprint(row.Schedule.Games) != "[2026020101 2026020102 2026020103]" {
		t.Errorf("the schedule was touched: %+v", row.Schedule)
	}
}

func TestAnUnchangedPanelIsNotPublishedToAgain(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early, late)
	for i := 0; i < 3; i++ {
		if err := h.Run(context.Background(), ""); err != nil {
			t.Fatal(err)
		}
		h.clock = h.clock.Add(time.Minute)
	}
	if len(h.pub.Messages) != 1 {
		t.Errorf("published %d messages over three minutes, want 1", len(h.pub.Messages))
	}
	// A panel with no schedule, and one nobody owns, are not on the list.
	h.panel(t, "scoreboard-02", "sub-a")
	h.panel(t, "scoreboard-03", "", early)
	if err := h.Run(context.Background(), ""); err != nil || len(h.pub.Messages) != 1 {
		t.Errorf("published %d, err %v", len(h.pub.Messages), err)
	}
}

func TestThePanelFollowsTheNightFromLiveToFinalToTheNextGame(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early, late)
	sent := func(want int64, when string) {
		t.Helper()
		if err := h.Run(context.Background(), ""); err != nil {
			t.Fatal(when, err)
		}
		if row := h.row(t, "scoreboard-01"); row.GameID != want {
			t.Errorf("%s: panel has %d, want %d", when, row.GameID, want)
		}
	}
	sent(early, "before the reducer sees it")
	h.game(early, "LIVE", time.Time{})
	sent(early, "live")
	// Past its 2h40 and still live: a live game is not cut off.
	h.clock = clock.Add(135 * time.Minute)
	sent(early, "long overtime")
	ended := h.clock.Add(5 * time.Minute)
	h.game(early, "FINAL", ended)
	h.clock = ended.Add(time.Minute)
	sent(early, "final, inside the hold, before the late puck drop")
	// The late game has started (the season's start has passed) though the
	// reducer has not caught up: puck drop is what cuts the hold short.
	h.clock = clock.Add(151 * time.Minute)
	sent(late, "late puck drop")
	h.game(late, "FINAL", h.clock.Add(150*time.Minute))
	h.clock = h.clock.Add(150*time.Minute + 3*time.Hour) // hold over, nothing ahead
	sent(late, "nothing ahead: nothing sent, the panel goes dark by its own rule")
	if n := len(h.pub.Messages); n != 2 {
		t.Errorf("published %d messages, want 2 (early, late)", n)
	}
}

func TestTheOwnersOverrideIsLeftAloneUntilItIsOver(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", late)
	ctx := context.Background()
	// The owner pressed "Show on panel" for the stranger; the API published
	// it and wrote the row. The director's marker does not match.
	row := h.row(t, "scoreboard-01")
	row.GameID, row.ChosenAt = stranger, clock.Add(-time.Minute).UnixMilli()
	_ = h.devices.Update(ctx, row)
	h.game(stranger, "LIVE", time.Time{})
	if err := h.Run(ctx, ""); err != nil || len(h.pub.Messages) != 0 {
		t.Fatalf("published %d, err %v: the owner's choice was not respected", len(h.pub.Messages), err)
	}
	if row := h.row(t, "scoreboard-01"); row.GameID != stranger || row.Sent != 0 {
		t.Errorf("row %+v was written", row)
	}
	// It ends, and the hold runs out: the schedule resumes.
	h.game(stranger, "FINAL", clock)
	h.clock = clock.Add(time.Duration(settings.BuiltIn.FinalHoldMin) * time.Minute)
	if err := h.Run(ctx, ""); err != nil || len(h.pub.Messages) != 1 {
		t.Fatalf("published %d, err %v", len(h.pub.Messages), err)
	}
	if got := decode(t, h.pub.Messages[0]); *got.GameID != late {
		t.Errorf("sent %d, want %d", *got.GameID, late)
	}
}

func TestOnlyTheNamedPanelIsDirectedByAnEvent(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early)
	h.panel(t, "scoreboard-02", "sub-a", early)
	h.panel(t, "scoreboard-03", "sub-a")
	ctx := context.Background()
	// A row nobody owns but that still carries a schedule (the Fake lets an
	// ownerless update through). The event names it; it is the one input
	// that comes from outside, and the owner check is all that refuses it.
	h.panel(t, "scoreboard-04", "")
	if err := h.devices.Update(ctx, devices.Device{ThingName: "scoreboard-04", Schedule: schedule.Panel{Games: []int64{early}}}); err != nil {
		t.Fatal(err)
	}
	for _, thing := range []string{"scoreboard-02", "scoreboard-03", "scoreboard-04", "scoreboard-99"} {
		if err := h.Run(ctx, thing); err != nil {
			t.Fatal(thing, err)
		}
	}
	if len(h.pub.Messages) != 1 || h.pub.Messages[0].Topic != panelconfig.Topic("scoreboard-02") {
		t.Errorf("messages %+v", h.pub.Messages)
	}
}

func TestAFailedPublishIsNotRecordedAndTheNextRunConverges(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early)
	h.pub.FailTopics = map[string]bool{panelconfig.Topic("scoreboard-01"): true}
	ctx := context.Background()
	if err := h.Run(ctx, ""); err == nil {
		t.Fatal("a failed publish was not reported")
	}
	if row := h.row(t, "scoreboard-01"); row.GameID != 0 || row.Sent != 0 {
		t.Errorf("recorded a game the panel never got: %+v", row)
	}
	h.pub.FailTopics = nil
	if err := h.Run(ctx, ""); err != nil || len(h.pub.Messages) != 1 {
		t.Fatalf("published %d, err %v", len(h.pub.Messages), err)
	}
	if row := h.row(t, "scoreboard-01"); row.GameID != early || row.Sent != early {
		t.Errorf("row %+v", row)
	}
}

// The other leg of publish-then-record: the panel got the document and the
// row did not get the write. The row must still name the old game, so the
// next run sends the same document again and records it -- a duplicate,
// never a panel the row disagrees with.
func TestAFailedRecordIsReportedAndTheNextRunConverges(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early)
	ctx := context.Background()
	h.Devices = failingDevices{h.devices, "scoreboard-01", nil}
	err := h.Run(ctx, "")
	if err == nil || !strings.Contains(err.Error(), "record") {
		t.Fatalf("err %v: a failed record was not reported", err)
	}
	if len(h.pub.Messages) != 1 {
		t.Fatalf("published %d, want 1: the publish comes before the record", len(h.pub.Messages))
	}
	if row := h.row(t, "scoreboard-01"); row.GameID != 0 || row.Sent != 0 || row.ChosenAt != 0 {
		t.Errorf("row %+v was written by a failed record", row)
	}
	h.Devices = h.devices
	if err := h.Run(ctx, ""); err != nil || len(h.pub.Messages) != 2 {
		t.Fatalf("published %d in all, err %v", len(h.pub.Messages), err)
	}
	if got := decode(t, h.pub.Messages[1]); *got.GameID != early {
		t.Errorf("sent %d again, want %d", *got.GameID, early)
	}
	if row := h.row(t, "scoreboard-01"); row.GameID != early || row.Sent != early {
		t.Errorf("row %+v", row)
	}
}

// The owner releases the panel in the moment between the director's read of
// the row and its write. The write is conditioned on the owner, so the
// released row is not given a game back and the run does not count it as
// a failure. The panel itself did receive the document (the publish came
// first), which is the stale retained document run.go says plainly it can
// carry until the next claim.
func TestAPanelReleasedDuringTheRunIsNotRecorded(t *testing.T) {
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early)
	h.panel(t, "scoreboard-02", "sub-a", early)
	ctx := context.Background()
	h.Devices = failingDevices{h.devices, "scoreboard-01", func() {
		if err := h.devices.Unbind(ctx, "scoreboard-01", "sub-a"); err != nil {
			t.Fatal(err)
		}
	}}
	if err := h.Run(ctx, ""); err != nil {
		t.Fatalf("err %v: a change of hands is not a failure", err)
	}
	if len(h.pub.Messages) != 2 {
		t.Fatalf("published %d, want 2", len(h.pub.Messages))
	}
	if row := h.row(t, "scoreboard-01"); row.Owner != "" || row.GameID != 0 || row.Sent != 0 || row.ChosenAt != 0 {
		t.Errorf("the released row %+v was handed a game", row)
	}
	if row := h.row(t, "scoreboard-02"); row.GameID != early || row.Sent != early {
		t.Errorf("the other panel's row %+v", row)
	}
	// Released, the panel is off the list: nothing more is sent to it.
	h.Devices = h.devices
	if err := h.Run(ctx, ""); err != nil || len(h.pub.Messages) != 2 {
		t.Errorf("published %d in all, err %v", len(h.pub.Messages), err)
	}
}

// failingDevices stands between the director and the Fake for one panel's
// record. With before set, it runs before the real write (a release in the
// gap, so the Fake's own owner condition refuses it); without, the write is
// refused outright, as a table that is down would.
type failingDevices struct {
	devices.Store
	thing  string
	before func()
}

func (f failingDevices) MarkSent(ctx context.Context, thingName, owner string, gameID, chosenAt int64) error {
	if thingName != f.thing {
		return f.Store.MarkSent(ctx, thingName, owner, gameID, chosenAt)
	}
	if f.before == nil {
		return errors.New("table down")
	}
	f.before()
	return f.Store.MarkSent(ctx, thingName, owner, gameID, chosenAt)
}

func TestNothingIsSentOnAGuess(t *testing.T) {
	// The season down: no kept set can be built, so no panel moves.
	h := newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early)
	h.Season = func(context.Context) (season.Season, error) { return season.Season{}, errors.New("down") }
	if err := h.Run(context.Background(), ""); err == nil || len(h.pub.Messages) != 0 {
		t.Errorf("published %d, err %v", len(h.pub.Messages), err)
	}
	// The games table down for one panel's game: that panel is skipped and
	// reported; the other is still directed.
	h = newHarness(t)
	h.panel(t, "scoreboard-01", "sub-a", early)
	h.panel(t, "scoreboard-02", "sub-a", late)
	h.Games = failingGames{h.games, early}
	err := h.Run(context.Background(), "")
	if err == nil || !strings.Contains(err.Error(), "scoreboard-01") {
		t.Errorf("err %v", err)
	}
	if len(h.pub.Messages) != 1 || h.pub.Messages[0].Topic != panelconfig.Topic("scoreboard-02") {
		t.Errorf("messages %+v", h.pub.Messages)
	}
}

type failingGames struct {
	gamestore.Store
	id int64
}

func (f failingGames) Get(ctx context.Context, id int64) (reduce.State, bool, error) {
	if id == f.id {
		return reduce.State{}, false, errors.New("table down")
	}
	return f.Store.Get(ctx, id)
}

func TestTheCeilingStopsARunFromStormingTheFleet(t *testing.T) {
	h := newHarness(t)
	for i := 0; i < MaxPublishes+5; i++ {
		h.panel(t, fmt.Sprintf("scoreboard-%02d", i), "sub-a", early)
	}
	if err := h.Run(context.Background(), ""); err != nil {
		t.Fatal(err)
	}
	if len(h.pub.Messages) != MaxPublishes {
		t.Errorf("published %d, want the ceiling %d", len(h.pub.Messages), MaxPublishes)
	}
	// The panels left over were not written either, so the next run picks
	// them up rather than believing they were sent.
	waiting := 0
	for i := 0; i < MaxPublishes+5; i++ {
		if h.row(t, fmt.Sprintf("scoreboard-%02d", i)).Sent == 0 {
			waiting++
		}
	}
	if waiting != 5 {
		t.Errorf("%d panels waiting, want 5", waiting)
	}
	if err := h.Run(context.Background(), ""); err != nil || len(h.pub.Messages) != MaxPublishes+5 {
		t.Errorf("next run: published %d in all, err %v", len(h.pub.Messages), err)
	}
}

// Every publish in every scenario above names a game from the panel's own
// kept set. Random schedules and states here, so the guarantee is checked
// against inputs nobody chose.
func TestNoPanelIsEverSentAGameOutsideItsKeptSet(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	all := []int64{early, late, tomorrow}
	kept := map[string]map[int64]bool{}
	for i := 0; i < 12; i++ {
		thing := fmt.Sprintf("scoreboard-%02d", i)
		var games []int64
		kept[thing] = map[int64]bool{}
		for j, id := range all {
			if (i>>j)&1 == 1 {
				games = append(games, id)
				kept[thing][id] = true
			}
		}
		h.panel(t, thing, "sub-a", games...)
		if i%4 == 3 { // an owner's override of a game not on the schedule
			row := h.row(t, thing)
			row.GameID = stranger
			_ = h.devices.Update(ctx, row)
		}
	}
	states := []string{"PRE", "LIVE", "FINAL"}
	for minute := 0; minute < 6*60; minute += 7 {
		h.clock = clock.Add(time.Duration(minute) * time.Minute)
		for k, id := range append(all, stranger) {
			h.game(id, states[(minute/30+k)%3], h.clock.Add(-10*time.Minute))
		}
		if err := h.Run(ctx, ""); err != nil {
			t.Fatal(err)
		}
	}
	if len(h.pub.Messages) == 0 {
		t.Fatal("nothing was published; the check checked nothing")
	}
	for _, m := range h.pub.Messages {
		thing := strings.TrimSuffix(strings.TrimPrefix(m.Topic, "scoreboard/"), "/config")
		if d := decode(t, m); d.GameID == nil || !kept[thing][*d.GameID] {
			t.Errorf("%s was sent %s, kept %v", thing, m.Payload, kept[thing])
		}
	}
}
