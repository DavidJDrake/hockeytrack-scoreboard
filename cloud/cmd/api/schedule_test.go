package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/season"
)

// Four games after the test clock: 1 and 2 overlap, 3 is later that night,
// 4 is the next day.
func testSeason(t *testing.T) season.Season {
	t.Helper()
	day := clock.AddDate(0, 0, 1).UTC().Format("2006-01-02")
	next := clock.AddDate(0, 0, 2).UTC().Format("2006-01-02")
	row := func(id int, at, away, home string) string {
		return fmt.Sprintf(`{"id":%d,"date":"`+at[:10]+`","start":"%s","away":"%s","home":"%s","type":2,"venue":"Rink"}`, id, at, away, home)
	}
	s, dropped, err := season.Parse([]byte(`{"teams":{"MTL":"Montréal Canadiens"},"games":[` + strings.Join([]string{
		row(2026020001, day+"T17:00:00Z", "MTL", "TOR"),
		row(2026020002, day+"T18:30:00Z", "BOS", "NYR"),
		row(2026020003, day+"T23:00:00Z", "EDM", "CGY"),
		row(2026020004, next+"T23:00:00Z", "VAN", "SEA"),
	}, ",") + `]}`))
	if err != nil || dropped != 0 {
		t.Fatal(err, dropped)
	}
	return s
}

func scheduleHandler(t *testing.T) (*Handler, *devices.Fake, *iotpub.Fake) {
	t.Helper()
	h, st, pub, _ := settingsHandler(t)
	sn := testSeason(t)
	h.Season = func(context.Context) (season.Season, error) { return sn, nil }
	return h, st, pub
}

func put(h *Handler, sub, panel, body string) (int, string) {
	res, _ := h.Handle(context.Background(), req("PUT", "PUT /api/devices/{thing}/schedule", sub, body, thing(panel)))
	return res.StatusCode, res.Body
}

func TestTheSeasonIsServedAsCheckedRows(t *testing.T) {
	h, _, _ := scheduleHandler(t)
	res, _ := h.Handle(context.Background(), req("GET", "GET /api/schedule", "sub-a", "", nil))
	var out struct {
		Teams map[string]string `json:"teams"`
		Games []season.Game     `json:"games"`
	}
	if err := json.Unmarshal([]byte(res.Body), &out); res.StatusCode != 200 || err != nil || len(out.Games) != 4 {
		t.Fatalf("%d %s", res.StatusCode, res.Body)
	}
	if out.Teams["MTL"] != "Montréal Canadiens" || out.Games[0].Date == "" {
		t.Errorf("club names and game dates are what the page groups and labels by: %s", res.Body[:200])
	}
	h.Season = func(context.Context) (season.Season, error) { return season.Season{}, errors.New("down") }
	if res, _ := h.Handle(context.Background(), req("GET", "GET /api/schedule", "sub-a", "", nil)); res.StatusCode != 502 {
		t.Errorf("a schedule source that is down: %d", res.StatusCode)
	}
}

func TestSavingGamesStoresThemAndPublishesNothing(t *testing.T) {
	h, st, pub := scheduleHandler(t)
	var asked []string
	h.Direct = func(_ context.Context, thing string) error { asked = append(asked, thing); return nil }
	status, body := put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020004,2026020001]}`)
	if status != 200 {
		t.Fatalf("%d %s", status, body)
	}
	d, _, _ := st.Get(context.Background(), "scoreboard-7qf2")
	if fmt.Sprint(d.Schedule.Games) != "[2026020001 2026020004]" {
		t.Errorf("stored %+v", d.Schedule)
	}
	// A panel follows gameId, and the director is what sets it. Saving a
	// schedule must not reach a panel by any route through this process;
	// it asks the director, once, for this panel.
	if len(pub.Messages) != 0 || d.GameID != 0 {
		t.Errorf("published %d messages, gameId %d", len(pub.Messages), d.GameID)
	}
	if fmt.Sprint(asked) != "[scoreboard-7qf2]" {
		t.Errorf("director asked for %v", asked)
	}
	var view scheduleView
	_ = json.Unmarshal([]byte(body), &view)
	if !view.Known || len(view.Next) != 2 || view.Next[0].Away != "MTL" {
		t.Errorf("view %s", body)
	}
}

func TestADirectorThatCannotBeReachedDoesNotFailTheSave(t *testing.T) {
	h, st, _ := scheduleHandler(t)
	h.Direct = func(context.Context, string) error { return errors.New("throttled") }
	if status, body := put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020001]}`); status != 200 {
		t.Fatalf("%d %s", status, body)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); len(d.Schedule.Games) != 1 {
		t.Errorf("stored %+v", d.Schedule)
	}
	// And a refused save asks for nothing: there is no change to act on.
	asked := 0
	h.Direct = func(context.Context, string) error { asked++; return nil }
	if status, _ := put(h, "sub-b", "scoreboard-7qf2", `{"games":[2026020001]}`); status != 404 || asked != 0 {
		t.Errorf("status %d, director asked %d times", status, asked)
	}
}

func TestAnotherAccountsPanelIsNotFoundAndNothingIsStored(t *testing.T) {
	h, st, _ := scheduleHandler(t)
	for _, panel := range []string{"scoreboard-bbbb", "scoreboard-cccc", "scoreboard-none"} {
		if status, body := put(h, "sub-a", panel, `{"games":[2026020001]}`); status != 404 {
			t.Errorf("%s: %d %s", panel, status, body)
		}
	}
	d, _, _ := st.Get(context.Background(), "scoreboard-bbbb")
	if !d.Schedule.IsZero() {
		t.Errorf("stored on somebody else's panel: %+v", d.Schedule)
	}
}

func TestAnUnansweredConflictIsA409AndNothingIsStored(t *testing.T) {
	h, st, _ := scheduleHandler(t)
	status, body := put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020001,2026020002,2026020004]}`)
	var out struct {
		Unresolved [][]int64 `json:"unresolved"`
	}
	_ = json.Unmarshal([]byte(body), &out)
	if status != 409 || fmt.Sprint(out.Unresolved) != "[[2026020001 2026020002]]" {
		t.Fatalf("%d %s", status, body)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); !d.Schedule.IsZero() {
		t.Errorf("stored despite the conflict: %+v", d.Schedule)
	}
	// Answered, it saves; and the answer decides what comes next.
	status, body = put(h, "sub-a", "scoreboard-7qf2",
		`{"games":[2026020001,2026020002,2026020004],"resolutions":[{"sequence":[2026020001,2026020002],"keep":[2026020002]}]}`)
	var view scheduleView
	_ = json.Unmarshal([]byte(body), &view)
	if status != 200 || len(view.Next) != 2 || view.Next[0].GameID != 2026020002 || len(view.Undecided) != 0 {
		t.Fatalf("%d %s", status, body)
	}
}

func TestWhatTheServerWillNotStore(t *testing.T) {
	h, st, _ := scheduleHandler(t)
	big := `{"games":[` + strings.Repeat("1,", 40000) + `1]}`
	for name, tc := range map[string]struct {
		body string
		want int
	}{
		"not JSON":                     {`games`, 400},
		"an unknown key":               {`{"games":[2026020001],"gameId":5}`, 400},
		"a game not in the season":     {`{"games":[2026029999]}`, 400},
		"an id of zero":                {`{"games":[0]}`, 400},
		"the same game twice":          {`{"games":[2026020001,2026020001]}`, 400},
		"an answer keeping both":       {`{"games":[2026020001,2026020002],"resolutions":[{"sequence":[2026020001,2026020002],"keep":[2026020001,2026020002]}]}`, 400},
		"a template, which nobody has": {`{"games":[],"templates":["t-1"]}`, 404},
		"a body over the bound":        {big, 400},
	} {
		if status, body := put(h, "sub-a", "scoreboard-7qf2", tc.body); status != tc.want {
			t.Errorf("%s: %d %s", name, status, body)
		}
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); !d.Schedule.IsZero() {
		t.Errorf("something was stored: %+v", d.Schedule)
	}
}

func TestWithoutTheSeasonNothingIsSavedOnAGuess(t *testing.T) {
	h, st, _ := scheduleHandler(t)
	h.Season = func(context.Context) (season.Season, error) { return season.Season{}, errors.New("down") }
	if status, _ := put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020001]}`); status != 502 {
		t.Errorf("got %d", status)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); !d.Schedule.IsZero() {
		t.Errorf("stored: %+v", d.Schedule)
	}
}

func TestThePanelListCarriesTheScheduleAndSurvivesTheSeasonBeingDown(t *testing.T) {
	h, _, _ := scheduleHandler(t)
	put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020003]}`)
	list := func() []deviceView {
		res, _ := h.Handle(context.Background(), req("GET", "GET /api/devices", "sub-a", "", nil))
		var out []deviceView
		if err := json.Unmarshal([]byte(res.Body), &out); res.StatusCode != 200 || err != nil {
			t.Fatalf("%d %s", res.StatusCode, res.Body)
		}
		return out
	}
	for _, d := range list() {
		if d.Schedule.Games == nil {
			t.Errorf("%s: games is null, which the site would have to guard against", d.ThingName)
		}
		if d.ThingName == "scoreboard-7qf2" && (!d.Schedule.Known || len(d.Schedule.Next) != 1) {
			t.Errorf("%+v", d.Schedule)
		}
	}
	h.Season = func(context.Context) (season.Season, error) { return season.Season{}, errors.New("down") }
	for _, d := range list() {
		if d.ThingName == "scoreboard-7qf2" && (d.Schedule.Known || fmt.Sprint(d.Schedule.Games) != "[2026020003]") {
			t.Errorf("season down: %+v", d.Schedule)
		}
	}
}

func TestOtherSavesCarryTheScheduleThrough(t *testing.T) {
	// Every route reads the row, changes its own field and writes the row.
	// A schedule must survive a rename, a game choice and a settings save.
	h, st, _ := scheduleHandler(t)
	ctx := context.Background()
	put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020003]}`)
	_, _ = h.Handle(ctx, req("PATCH", "PATCH /api/devices/{thing}", "sub-a", `{"name":"Den"}`, thing("scoreboard-7qf2")))
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`, thing("scoreboard-7qf2")))
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"finalHoldMin":30}`, thing("scoreboard-7qf2")))
	if d, _, _ := st.Get(ctx, "scoreboard-7qf2"); fmt.Sprint(d.Schedule.Games) != "[2026020003]" || d.Name != "Den" {
		t.Errorf("%+v", d)
	}
}
