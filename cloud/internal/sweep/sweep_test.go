package sweep

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/presence"
)

var now = time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)

func TestTheRuleNamesOnlyAnUnownedPanelUnseenForOverAYear(t *testing.T) {
	old := now.Add(-Unseen - time.Hour)
	for _, tc := range []struct {
		name string
		dev  devices.Device
		seen *presence.Seen
		want bool
	}{
		{"unowned, off the broker for over a year", devices.Device{ThingName: "p"}, &presence.Seen{At: old}, true},
		{"owned", devices.Device{ThingName: "p", Owner: "sub-a"}, &presence.Seen{At: old}, false},
		{"released through the list: that path's to handle", devices.Device{ThingName: "p", ReleasedBy: "sub-a"}, &presence.Seen{At: old}, false},
		{"on the broker now, whatever the connect time says", devices.Device{ThingName: "p"}, &presence.Seen{Connected: true, At: old}, false},
		{"no record in the index at all", devices.Device{ThingName: "p"}, nil, false},
		{"never connected since indexing began: unknown, not old", devices.Device{ThingName: "p"}, &presence.Seen{}, false},
		{"exactly a year: not older than", devices.Device{ThingName: "p"}, &presence.Seen{At: now.Add(-Unseen)}, false},
		{"a second past a year", devices.Device{ThingName: "p"}, &presence.Seen{At: now.Add(-Unseen - time.Second)}, true},
		{"seen yesterday", devices.Device{ThingName: "p"}, &presence.Seen{At: now.Add(-24 * time.Hour)}, false},
		{"a record from the future: a clock is wrong, so nothing", devices.Device{ThingName: "p"}, &presence.Seen{At: now.Add(time.Hour)}, false},
	} {
		seen := map[string]presence.Seen{}
		if tc.seen != nil {
			seen["p"] = *tc.seen
		}
		chosen, past := Select([]devices.Device{tc.dev}, seen, now)
		if got := len(chosen) == 1; got != tc.want || len(past) != 0 {
			t.Errorf("%s: selected=%v past=%d, want selected=%v", tc.name, got, len(past), tc.want)
		}
	}
}

func TestTheCeilingHoldsAndSaysSo(t *testing.T) {
	var devs []devices.Device
	seen := map[string]presence.Seen{}
	for i := 0; i < MaxRetire+3; i++ {
		name := fmt.Sprintf("scoreboard-%02d", i)
		devs = append(devs, devices.Device{ThingName: name})
		seen[name] = presence.Seen{At: now.Add(-2 * Unseen)}
	}
	chosen, past := Select(devs, seen, now)
	if len(chosen) != MaxRetire || len(past) != 3 {
		t.Fatalf("selected %d, past %d; want %d and 3", len(chosen), len(past), MaxRetire)
	}
	// Deterministic: the first MaxRetire by name, so two runs on the same
	// facts name the same panels, and the rest are the rest in order.
	for i, c := range append(chosen, past...) {
		if want := fmt.Sprintf("scoreboard-%02d", i); c.ThingName != want {
			t.Errorf("[%d] = %s, want %s", i, c.ThingName, want)
		}
	}
	// Exactly the ceiling is not over it.
	if chosen, past := Select(devs[:MaxRetire], seen, now); len(chosen) != MaxRetire || len(past) != 0 {
		t.Errorf("at the ceiling: selected %d, past %d", len(chosen), len(past))
	}
}

// In dry-run nothing changes between runs, so the same MaxRetire would be
// cut every day and a panel past the cut would never be in the record. The
// run names it anyway, on a line the would-retire metric does not count.
func TestARunNamesThePanelsPastTheCeilingWithoutCountingThem(t *testing.T) {
	st, ctx := devices.NewFake(), context.Background()
	idx := presence.Fake{}
	for i := 0; i < MaxRetire+1; i++ {
		name := fmt.Sprintf("scoreboard-%02d", i)
		_ = st.Register(ctx, name)
		idx[name] = presence.Seen{At: now.Add(-2 * Unseen)}
	}
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })

	s := &Sweep{Devices: st, Presence: idx, Now: func() time.Time { return now }}
	chosen, err := s.Run(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(chosen) != MaxRetire {
		t.Fatalf("returned %d, want the %d act mode would have taken", len(chosen), MaxRetire)
	}
	log := buf.String()
	// The metric filters match their phrase anywhere in an event, so the
	// count of would-retire lines is the count of the phrase, and the past
	// line must not carry it or the ceiling phrase.
	if strings.Contains(PastCeilingMessage, WouldRetireMessage) || strings.Contains(PastCeilingMessage, "retire ceiling reached") {
		t.Errorf("PastCeilingMessage %q would be counted by another filter", PastCeilingMessage)
	}
	if n := strings.Count(log, WouldRetireMessage); n != MaxRetire {
		t.Errorf("%d would-retire lines, want %d:\n%s", n, MaxRetire, log)
	}
	if n := strings.Count(log, "retire ceiling reached"); n != 1 {
		t.Errorf("%d ceiling lines, want 1:\n%s", n, log)
	}
	last := fmt.Sprintf("scoreboard-%02d", MaxRetire)
	if !strings.Contains(log, PastCeilingMessage) || !strings.Contains(log, "thing="+last) {
		t.Errorf("%s is past the ceiling and not in the record:\n%s", last, log)
	}
}

func TestARunReadsOnlyTheUnownedAndActsOnNothing(t *testing.T) {
	st, ctx := devices.NewFake(), context.Background()
	for _, name := range []string{"scoreboard-01", "scoreboard-02", "scoreboard-03"} {
		_ = st.Register(ctx, name)
	}
	_ = st.Claim(ctx, "scoreboard-02", "sub-a")
	old := now.Add(-2 * Unseen)
	idx := presence.Fake{
		"scoreboard-01": {At: old},
		"scoreboard-02": {At: old}, // owned: never asked about, never named
		"scoreboard-03": {Connected: true, At: old},
	}
	s := &Sweep{Devices: st, Presence: idx, Now: func() time.Time { return now }}
	chosen, err := s.Run(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(chosen) != 1 || chosen[0].ThingName != "scoreboard-01" || !chosen[0].LastSeen.Equal(old) {
		t.Fatalf("chosen = %+v", chosen)
	}
	// Dry-run: every row is exactly as it was.
	for _, name := range []string{"scoreboard-01", "scoreboard-02", "scoreboard-03"} {
		d, found, _ := st.Get(ctx, name)
		if !found {
			t.Errorf("%s is gone", name)
		}
		if name == "scoreboard-02" && d.Owner != "sub-a" {
			t.Errorf("%s lost its owner", name)
		}
	}
}

func TestARunWithNoUnownedPanelsAsksTheIndexNothing(t *testing.T) {
	st, ctx := devices.NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01")
	_ = st.Claim(ctx, "scoreboard-01", "sub-a")
	s := &Sweep{Devices: st, Presence: refusing{}, Now: func() time.Time { return now }}
	if chosen, err := s.Run(ctx); err != nil || len(chosen) != 0 {
		t.Fatalf("%+v %v", chosen, err)
	}
}

func TestAFailedIndexReadStopsTheRun(t *testing.T) {
	st, ctx := devices.NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01")
	s := &Sweep{Devices: st, Presence: refusing{}, Now: func() time.Time { return now }}
	if _, err := s.Run(ctx); err == nil {
		t.Fatal("a run that could not read the broker's record judged anyway")
	}
}

// refusing is a Source that fails every lookup.
type refusing struct{}

func (refusing) Lookup(context.Context, []string) (map[string]presence.Seen, error) {
	return nil, fmt.Errorf("index unavailable")
}
