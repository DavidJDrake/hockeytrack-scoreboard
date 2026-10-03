package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/season"
	"hockeytrack-scoreboard/internal/templates"
)

func templatesHandler(t *testing.T) (*Handler, *devices.Fake, *templates.Fake) {
	t.Helper()
	h, st, _ := scheduleHandler(t)
	tp := templates.NewFake()
	h.Templates = tp
	return h, st, tp
}

func call(h *Handler, method, route, sub, body string, params map[string]string) (int, string) {
	res, _ := h.Handle(context.Background(), req(method, route, sub, body, params))
	return res.StatusCode, res.Body
}

func tid(id string) map[string]string { return map[string]string{"id": id} }

// create makes a template for sub and returns its id, failing the test if
// the route does not.
func create(t *testing.T, h *Handler, sub, body string) string {
	t.Helper()
	status, res := call(h, "POST", "POST /api/templates", sub, body, nil)
	var out templates.Template
	if err := json.Unmarshal([]byte(res), &out); status != 201 || err != nil || out.ID == "" {
		t.Fatalf("%d %s", status, res)
	}
	return out.ID
}

func list(t *testing.T, h *Handler, sub string) []templates.Template {
	t.Helper()
	status, res := call(h, "GET", "GET /api/templates", sub, "", nil)
	var out []templates.Template
	if err := json.Unmarshal([]byte(res), &out); status != 200 || err != nil || out == nil {
		t.Fatalf("%d %s", status, res)
	}
	return out
}

func TestATemplateIsMadeListedEditedAndDeletedByItsOwner(t *testing.T) {
	h, _, _ := templatesHandler(t)
	id := create(t, h, "sub-a", `{"name":"Habs","games":[2026020004,2026020001]}`)
	if len(id) != 24 {
		t.Errorf("the id is the server's: %q", id)
	}
	got := list(t, h, "sub-a")
	if len(got) != 1 || got[0].ID != id || got[0].Name != "Habs" || fmt.Sprint(got[0].Games) != "[2026020001 2026020004]" {
		t.Errorf("%+v", got)
	}
	status, res := call(h, "PUT", "PUT /api/templates/{id}", "sub-a", `{"name":"Habs, renamed","games":[2026020003]}`, tid(id))
	var edited struct {
		templates.Template
		Panels []affectedPanel `json:"panels"`
	}
	if err := json.Unmarshal([]byte(res), &edited); status != 200 || err != nil || edited.ID != id || edited.Name != "Habs, renamed" || fmt.Sprint(edited.Games) != "[2026020003]" {
		t.Fatalf("%d %s", status, res)
	}
	if edited.Panels == nil || len(edited.Panels) != 0 {
		t.Errorf("no panel uses it, and the list says so rather than being null: %s", res)
	}
	if status, res := call(h, "DELETE", "DELETE /api/templates/{id}", "sub-a", "", tid(id)); status != 200 || !strings.Contains(res, id) {
		t.Errorf("%d %s", status, res)
	}
	if got := list(t, h, "sub-a"); len(got) != 0 {
		t.Errorf("still listed: %+v", got)
	}
}

func TestAnotherAccountsTemplateIsNotFoundNeverForbidden(t *testing.T) {
	h, st, tp := templatesHandler(t)
	id := create(t, h, "sub-b", `{"name":"Theirs","games":[2026020001]}`)
	if got := list(t, h, "sub-a"); len(got) != 0 {
		t.Errorf("sub-a sees %+v", got)
	}
	// Every route that takes an id answers the same for somebody else's
	// template as for one that does not exist, and changes nothing.
	for name, tc := range map[string]struct {
		method, route, body string
	}{
		"edit":   {"PUT", "PUT /api/templates/{id}", `{"name":"Mine now","games":[]}`},
		"delete": {"DELETE", "DELETE /api/templates/{id}", ""},
	} {
		for _, target := range []string{id, "000000000000000000000000", "", "../" + id} {
			if status, res := call(h, tc.method, tc.route, "sub-a", tc.body, tid(target)); status != 404 || !strings.Contains(res, "no such template") {
				t.Errorf("%s of %q: %d %s", name, target, status, res)
			}
		}
	}
	// Nor can it be attached to sub-a's panel.
	if status, res := put(h, "sub-a", "scoreboard-7qf2", `{"games":[],"templates":["`+id+`"]}`); status != 404 {
		t.Errorf("attach: %d %s", status, res)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); !d.Schedule.IsZero() {
		t.Errorf("stored: %+v", d.Schedule)
	}
	got, _ := tp.List(context.Background(), "sub-b")
	if len(got) != 1 || got[0].Name != "Theirs" {
		t.Errorf("sub-b's template was touched: %+v", got)
	}
}

func TestWhatTheServerWillNotStoreAsATemplate(t *testing.T) {
	h, _, _ := templatesHandler(t)
	ids := func(n int) string {
		parts := make([]string, n)
		for i := range parts {
			parts[i] = fmt.Sprint(2026020001 + i%4) // every id in the season
		}
		return strings.Join(parts, ",")
	}
	for name, tc := range map[string]struct {
		body string
		want int
		msg  string
	}{
		"not JSON":                   {`name`, 400, "invalid template"},
		"an id of its own choosing":  {`{"id":"mine","name":"x","games":[]}`, 400, "invalid template"},
		"a game not in the season":   {`{"name":"x","games":[2026029999]}`, 400, "unknown game"},
		"one known, one not":         {`{"name":"x","games":[2026020001,2026029999]}`, 400, "unknown game"},
		"no name":                    {`{"games":[2026020001]}`, 400, "invalid name"},
		"a name over the bound":      {`{"name":"` + strings.Repeat("x", templates.MaxNameRunes+1) + `","games":[]}`, 400, "invalid name"},
		"a name with a line break":   {`{"name":"a\nb","games":[]}`, 400, "invalid name"},
		"a body over the bound":      {`{"name":"x","games":[` + ids(templates.MaxBody/5) + `]}`, 400, "invalid template"},
		"a name at the bound":        {`{"name":"` + strings.Repeat("x", templates.MaxNameRunes) + `","games":[]}`, 201, ""},
		"no games at all":            {`{"name":"Empty"}`, 201, ""},
		"a game already in the list": {`{"name":"x","games":[2026020001,2026020001]}`, 400, "invalid template"},
	} {
		status, res := call(h, "POST", "POST /api/templates", "sub-a", tc.body, nil)
		if status != tc.want || !strings.Contains(res, tc.msg) {
			t.Errorf("%s: %d %s", name, status, res)
		}
	}
	// The season down: nothing is stored on a guess.
	h.Season = func(context.Context) (season.Season, error) { return season.Season{}, errors.New("down") }
	if status, _ := call(h, "POST", "POST /api/templates", "sub-a", `{"name":"x","games":[2026020001]}`, nil); status != 502 {
		t.Errorf("season down: %d", status)
	}
	if got := list(t, h, "sub-a"); len(got) != 2 {
		t.Errorf("stored: %+v", got)
	}
}

func TestABodyOverTheBoundIsRefusedForItsSizeAlone(t *testing.T) {
	h, _, _ := templatesHandler(t)
	id := create(t, h, "sub-a", `{"name":"Kept","games":[]}`)
	// The body is a valid template padded with whitespace past the bound:
	// nothing about it is wrong but its size, and the decoder would take it
	// (trailing whitespace is not a second value), so only the size check
	// stands between it and the store. One byte under is stored.
	over := `{"name":"x","games":[]}` + strings.Repeat(" ", templates.MaxBody)
	under := `{"name":"y","games":[]}` + strings.Repeat(" ", templates.MaxBody-len(`{"name":"y","games":[]}`))
	if len(under) != templates.MaxBody {
		t.Fatalf("the at-bound body is %d bytes", len(under))
	}
	for name, tc := range map[string]struct {
		method, route string
		params        map[string]string
	}{
		"create": {"POST", "POST /api/templates", nil},
		"edit":   {"PUT", "PUT /api/templates/{id}", tid(id)},
	} {
		if status, res := call(h, tc.method, tc.route, "sub-a", over, tc.params); status != 400 || !strings.Contains(res, "invalid template") {
			t.Errorf("%s over: %d %s", name, status, res)
		}
		if status, res := call(h, tc.method, tc.route, "sub-a", under, tc.params); status/100 != 2 {
			t.Errorf("%s at the bound: %d %s", name, status, res)
		}
	}
	// The over-size bodies were never stored: the account holds the one made
	// here, then the one the at-bound create added, and the at-bound edit
	// renamed the first.
	got := list(t, h, "sub-a")
	if len(got) != 2 || got[0].Name != "y" || got[1].Name != "y" {
		t.Errorf("stored: %+v", got)
	}
}

func TestAnAccountHoldsAtMostTheBoundOfTemplates(t *testing.T) {
	h, _, _ := templatesHandler(t)
	for i := 0; i < templates.MaxPerAccount; i++ {
		create(t, h, "sub-a", fmt.Sprintf(`{"name":"Set %d","games":[]}`, i))
	}
	status, res := call(h, "POST", "POST /api/templates", "sub-a", `{"name":"One more","games":[]}`, nil)
	if status != 409 || !strings.Contains(res, `"max":20`) {
		t.Errorf("%d %s", status, res)
	}
	// The bound is per account: another account starts from nothing.
	create(t, h, "sub-b", `{"name":"Theirs","games":[]}`)
	// And deleting one makes room.
	got := list(t, h, "sub-a")
	if status, _ := call(h, "DELETE", "DELETE /api/templates/{id}", "sub-a", "", tid(got[0].ID)); status != 200 {
		t.Fatal(status)
	}
	create(t, h, "sub-a", `{"name":"Room again","games":[]}`)
}

func TestAnIdCollisionIsRetriedOnceThenRefused(t *testing.T) {
	h, _, _ := templatesHandler(t)
	ids := []string{"aaaaaaaaaaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbb", "bbbbbbbbbbbbbbbbbbbbbbbb", "bbbbbbbbbbbbbbbbbbbbbbbb"}
	h.NewID = func() string { id := ids[0]; ids = ids[1:]; return id }
	create(t, h, "sub-a", `{"name":"First","games":[]}`)
	if id := create(t, h, "sub-a", `{"name":"Second","games":[]}`); id != "bbbbbbbbbbbbbbbbbbbbbbbb" {
		t.Errorf("the collision was not retried: %q", id)
	}
	if status, _ := call(h, "POST", "POST /api/templates", "sub-a", `{"name":"Third","games":[]}`, nil); status != 500 {
		t.Errorf("a second collision: %d", status)
	}
}

func TestATemplateInUseIsNotDeletedAndTheAnswerSaysHowMany(t *testing.T) {
	h, st, _ := templatesHandler(t)
	id := create(t, h, "sub-a", `{"name":"Habs","games":[2026020003]}`)
	for _, panel := range []string{"scoreboard-7qf2", "scoreboard-aaaa"} {
		if status, res := put(h, "sub-a", panel, `{"games":[],"templates":["`+id+`"]}`); status != 200 {
			t.Fatalf("%s: %d %s", panel, status, res)
		}
	}
	status, res := call(h, "DELETE", "DELETE /api/templates/{id}", "sub-a", "", tid(id))
	var out struct {
		Error  string   `json:"error"`
		Panels int      `json:"panels"`
		Things []string `json:"thingNames"`
	}
	if err := json.Unmarshal([]byte(res), &out); status != 409 || err != nil || out.Panels != 2 || len(out.Things) != 2 {
		t.Fatalf("%d %s", status, res)
	}
	if got := list(t, h, "sub-a"); len(got) != 1 {
		t.Errorf("deleted anyway: %+v", got)
	}
	// Detached from one panel, it is still in use by the other.
	put(h, "sub-a", "scoreboard-7qf2", `{"games":[]}`)
	if status, res := call(h, "DELETE", "DELETE /api/templates/{id}", "sub-a", "", tid(id)); status != 409 || !strings.Contains(res, `"panels":1`) {
		t.Errorf("%d %s", status, res)
	}
	put(h, "sub-a", "scoreboard-aaaa", `{"games":[]}`)
	if status, _ := call(h, "DELETE", "DELETE /api/templates/{id}", "sub-a", "", tid(id)); status != 200 {
		t.Errorf("detached everywhere: %d", status)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-aaaa"); len(d.Schedule.Templates) != 0 {
		t.Errorf("%+v", d.Schedule)
	}
}

func TestAPanelShowsItsTemplatesGamesAndItsOwnWinAnUnansweredOverlap(t *testing.T) {
	h, st, _ := templatesHandler(t)
	// Games 1 and 2 overlap; 4 is the next day.
	id := create(t, h, "sub-a", `{"name":"Habs","games":[2026020002,2026020004]}`)
	// The template alone: its games are what comes next.
	status, body := put(h, "sub-a", "scoreboard-7qf2", `{"games":[],"templates":["`+id+`"]}`)
	var view scheduleView
	_ = json.Unmarshal([]byte(body), &view)
	if status != 200 || !view.Known || len(view.Next) != 2 || view.Next[0].GameID != 2026020002 || fmt.Sprint(view.Templates) != "["+id+"]" {
		t.Fatalf("%d %s", status, body)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); fmt.Sprint(d.Schedule.Templates) != "["+id+"]" {
		t.Errorf("stored %+v", d.Schedule)
	}
	// Adding the panel's own game 1 conflicts with the template's 2: the
	// owner is here, so the save is refused until they answer.
	status, body = put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020001],"templates":["`+id+`"]}`)
	if status != 409 || !strings.Contains(body, "[[2026020001,2026020002]]") {
		t.Fatalf("%d %s", status, body)
	}
	status, body = put(h, "sub-a", "scoreboard-7qf2",
		`{"games":[2026020001],"templates":["`+id+`"],"resolutions":[{"sequence":[2026020001,2026020002],"keep":[2026020002]}]}`)
	_ = json.Unmarshal([]byte(body), &view)
	if status != 200 || len(view.Next) != 2 || view.Next[0].GameID != 2026020002 || len(view.Undecided) != 0 {
		t.Fatalf("%d %s", status, body)
	}
	// The panel list shows the same, and says less when the templates
	// cannot be read: a kept set without the template games would be untrue.
	panels := func() scheduleView {
		res, _ := h.Handle(context.Background(), req("GET", "GET /api/devices", "sub-a", "", nil))
		var out []deviceView
		_ = json.Unmarshal([]byte(res.Body), &out)
		for _, d := range out {
			if d.ThingName == "scoreboard-7qf2" {
				return d.Schedule
			}
		}
		t.Fatalf("%d %s", res.StatusCode, res.Body)
		return scheduleView{}
	}
	if v := panels(); !v.Known || len(v.Next) != 2 || v.Next[0].GameID != 2026020002 {
		t.Errorf("%+v", v)
	}
	h.Templates.(*templates.Fake).Err = errors.New("down")
	if v := panels(); v.Known || fmt.Sprint(v.Templates) != "["+id+"]" {
		t.Errorf("templates down: %+v", v)
	}
}

func TestEditingATemplateReportsThePanelsItChangedAndAsksTheDirector(t *testing.T) {
	h, _, _ := templatesHandler(t)
	var asked []string
	h.Direct = func(_ context.Context, thing string) error { asked = append(asked, thing); return nil }
	id := create(t, h, "sub-a", `{"name":"Habs","games":[2026020004]}`)
	other := create(t, h, "sub-a", `{"name":"Other","games":[2026020004]}`)
	put(h, "sub-a", "scoreboard-7qf2", `{"games":[2026020001],"templates":["`+id+`"]}`)
	put(h, "sub-a", "scoreboard-aaaa", `{"games":[],"templates":["`+other+`"]}`)
	asked = nil
	// The edit puts game 2 in the template, which overlaps game 1 on the
	// first panel. That panel now needs a decision; the second is untouched.
	status, res := call(h, "PUT", "PUT /api/templates/{id}", "sub-a", `{"name":"Habs","games":[2026020002,2026020004]}`, tid(id))
	var out struct {
		Panels []affectedPanel `json:"panels"`
	}
	if err := json.Unmarshal([]byte(res), &out); status != 200 || err != nil || len(out.Panels) != 1 {
		t.Fatalf("%d %s", status, res)
	}
	p := out.Panels[0]
	if p.ThingName != "scoreboard-7qf2" || !p.NeedsDecision || fmt.Sprint(p.Undecided) != "[[2026020001 2026020002]]" {
		t.Errorf("%+v", p)
	}
	if fmt.Sprint(asked) != "[scoreboard-7qf2]" {
		t.Errorf("director asked for %v", asked)
	}
	// Nothing was decided for the owner, but the panel is not undecided
	// either: its own game wins until they answer.
	res2, _ := h.Handle(context.Background(), req("GET", "GET /api/devices", "sub-a", "", nil))
	var views []deviceView
	_ = json.Unmarshal([]byte(res2.Body), &views)
	for _, v := range views {
		if v.ThingName == "scoreboard-7qf2" && (len(v.Schedule.Undecided) != 1 || v.Schedule.Next[0].GameID != 2026020001) {
			t.Errorf("%+v", v.Schedule)
		}
	}
	// A game the template already held that has left the season is dropped
	// without complaint; a new one not in the season is refused.
	h.Templates.(*templates.Fake).Replace(context.Background(), "sub-a", templates.Template{ID: id, Name: "Habs", Games: []int64{2026020004, 2026029998}})
	if status, res := call(h, "PUT", "PUT /api/templates/{id}", "sub-a", `{"name":"Habs","games":[2026020004,2026029998]}`, tid(id)); status != 200 || strings.Contains(res, "2026029998") {
		t.Errorf("a past game: %d %s", status, res)
	}
	if status, _ := call(h, "PUT", "PUT /api/templates/{id}", "sub-a", `{"name":"Habs","games":[2026029999]}`, tid(id)); status != 400 {
		t.Errorf("an unknown game: %d", status)
	}
}

func TestWithoutATemplatesStoreTheRoutesSaySoAndASchedulePanelHasNone(t *testing.T) {
	h, st, _ := scheduleHandler(t)
	for _, tc := range []struct{ method, route string }{
		{"GET", "GET /api/templates"}, {"POST", "POST /api/templates"},
		{"PUT", "PUT /api/templates/{id}"}, {"DELETE", "DELETE /api/templates/{id}"},
	} {
		if status, res := call(h, tc.method, tc.route, "sub-a", `{"name":"x","games":[]}`, tid("abc")); status != 500 || !strings.Contains(res, "not configured") {
			t.Errorf("%s: %d %s", tc.route, status, res)
		}
	}
	// With no store the caller has none, so a schedule cannot name one.
	if status, res := put(h, "sub-a", "scoreboard-7qf2", `{"games":[],"templates":["abc"]}`); status != 404 || !strings.Contains(res, "no such template") {
		t.Errorf("attach: %d %s", status, res)
	}
	// A row that names a template anyway (written under a deployment that
	// had the table) is listed, but its kept set is not: the template's
	// games cannot be had, and a kept set built from the panel's own games
	// alone would be one the owner did not ask for. The same answer as when
	// the table is down, and what the director does with such a row.
	ctx := context.Background()
	d, _, _ := st.Get(ctx, "scoreboard-7qf2")
	d.Schedule.Games, d.Schedule.Templates = []int64{2026020001}, []string{"abc"}
	if err := st.Update(ctx, d); err != nil {
		t.Fatal(err)
	}
	res, _ := h.Handle(ctx, req("GET", "GET /api/devices", "sub-a", "", nil))
	var views []deviceView
	if err := json.Unmarshal([]byte(res.Body), &views); res.StatusCode != 200 || err != nil {
		t.Fatalf("%d %s", res.StatusCode, res.Body)
	}
	for _, v := range views {
		if v.ThingName == "scoreboard-7qf2" && (v.Schedule.Known || len(v.Schedule.Next) != 0 || fmt.Sprint(v.Schedule.Templates) != "[abc]") {
			t.Errorf("no store, a row naming a template: %+v", v.Schedule)
		}
	}
}
