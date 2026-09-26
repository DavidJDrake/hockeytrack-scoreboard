package main

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"testing"

	"hockeytrack-scoreboard/internal/accounts"
	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/iotpub"
)

func settingsHandler(t *testing.T) (*Handler, *devices.Fake, *iotpub.Fake, *accounts.Fake) {
	t.Helper()
	h, st, pub := handlerWith(t)
	acc := accounts.NewFake()
	h.Accounts = acc
	for _, name := range []string{"scoreboard-aaaa", "scoreboard-bbbb", "scoreboard-cccc"} {
		_ = st.Register(context.Background(), name)
	}
	_ = st.Claim(context.Background(), "scoreboard-7qf2", "sub-a")
	_ = st.Claim(context.Background(), "scoreboard-aaaa", "sub-a")
	_ = st.Claim(context.Background(), "scoreboard-bbbb", "sub-b")
	return h, st, pub, acc
}

func thing(name string) map[string]string { return map[string]string{"thing": name} }

const sleepBody = `{"sleep":{"enabled":true,"start":"23:00","end":"07:00","zone":"America/Toronto"}}`

func TestSavingAPanelsSettingsSendsTheWholeDocumentWithTheSameStamp(t *testing.T) {
	h, _, pub, _ := settingsHandler(t)
	ctx := context.Background()
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`, thing("scoreboard-7qf2")))
	stamp := clock.UnixMilli()
	clock = clock.Add(3 * 60 * 60 * 1e9) // three hours later
	defer func() { clock = clock.Add(-3 * 60 * 60 * 1e9) }()

	res, _ := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", sleepBody, thing("scoreboard-7qf2")))
	if res.StatusCode != 200 {
		t.Fatalf("status %d: %s", res.StatusCode, res.Body)
	}
	m := pub.Messages[len(pub.Messages)-1]
	if m.Topic != "scoreboard/scoreboard-7qf2/config" || !m.Retain {
		t.Errorf("published to %q retain=%v", m.Topic, m.Retain)
	}
	var doc struct {
		GameID   int64 `json:"gameId"`
		ChosenAt int64 `json:"chosenAt"`
		Display  struct {
			Sleep *struct{ Zone string } `json:"sleep"`
		} `json:"display"`
	}
	_ = json.Unmarshal(m.Payload, &doc)
	if doc.GameID != 2026020001 {
		t.Errorf("the game was dropped from the document: %s", m.Payload)
	}
	// A panel reads a stamp it has not seen as the owner pressing a button.
	// Saving sleep hours is not that.
	if doc.ChosenAt != stamp {
		t.Errorf("chosenAt = %d, want the stored %d: saving settings would look like a press", doc.ChosenAt, stamp)
	}
	if doc.Display.Sleep == nil || doc.Display.Sleep.Zone != "America/Toronto" {
		t.Errorf("settings missing from the document: %s", m.Payload)
	}
}

func TestChoosingAGameKeepsTheSettingsInTheDocument(t *testing.T) {
	// The document is one retained message. Before this, choosing a game
	// published {gameId, chosenAt} and would have wiped the settings from
	// the panel's next reconnect.
	h, _, pub, _ := settingsHandler(t)
	ctx := context.Background()
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"finalHoldMin":30}`, thing("scoreboard-7qf2")))
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`, thing("scoreboard-7qf2")))
	if got := string(pub.Messages[len(pub.Messages)-1].Payload); !strings.Contains(got, `"finalHoldMin":30`) {
		t.Errorf("choosing a game dropped the settings: %s", got)
	}
}

func TestAPanelFollowingNothingIsSentNoGame(t *testing.T) {
	h, _, pub, _ := settingsHandler(t)
	_, _ = h.Handle(context.Background(), req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"finalHoldMin":30}`, thing("scoreboard-7qf2")))
	if got := string(pub.Messages[0].Payload); !strings.HasPrefix(got, `{"gameId":null,`) {
		t.Errorf("document = %s; a panel reads gameId 0 as a game to select", got)
	}
}

func TestSettingsAreValidatedAndAClientCannotSmuggleAKey(t *testing.T) {
	h, _, pub, _ := settingsHandler(t)
	for name, body := range map[string]string{
		"out of range":     `{"countdownLeadMin":99999}`,
		"a bad zone":       `{"sleep":{"enabled":true,"start":"23:00","end":"07:00","zone":"../../etc/passwd"}}`,
		"a smuggled game":  `{"gameId":1,"finalHoldMin":30}`,
		"a smuggled stamp": `{"chosenAt":1,"finalHoldMin":30}`,
		"a display block":  `{"display":{"v":1}}`,
		"not json":         `finalHoldMin=30`,
	} {
		for _, route := range []struct {
			method, key string
			params      map[string]string
		}{
			{"PUT", "PUT /api/devices/{thing}/display", thing("scoreboard-7qf2")},
			{"PUT", "PUT /api/settings", nil},
		} {
			res, _ := h.Handle(context.Background(), req(route.method, route.key, "sub-a", body, route.params))
			if res.StatusCode != 400 {
				t.Errorf("%s on %s: status %d", name, route.key, res.StatusCode)
			}
		}
	}
	if len(pub.Messages) != 0 {
		t.Errorf("a refused request published %d messages", len(pub.Messages))
	}
}

func TestABodyTooLargeToBeSettingsIsRefused(t *testing.T) {
	h, _, _, _ := settingsHandler(t)
	big := `{"finalHoldMin":30` + strings.Repeat(" ", 5000) + `}`
	res, _ := h.Handle(context.Background(), req("PUT", "PUT /api/settings", "sub-a", big, nil))
	if res.StatusCode != 400 {
		t.Errorf("status %d", res.StatusCode)
	}
}

func TestSomeoneElsesPanelIsNotFound(t *testing.T) {
	h, st, pub, _ := settingsHandler(t)
	res, _ := h.Handle(context.Background(), req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"finalHoldMin":30}`, thing("scoreboard-bbbb")))
	if res.StatusCode != 404 {
		t.Errorf("status %d, want 404 (never 403: ids must not be probeable)", res.StatusCode)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-bbbb"); d.Display.FinalHoldMin != nil || len(pub.Messages) != 0 {
		t.Error("somebody else's panel was changed or published to")
	}
	// And one nobody has claimed, and one that does not exist: the same answer.
	for _, name := range []string{"scoreboard-cccc", "scoreboard-zzzz"} {
		if res, _ := h.Handle(context.Background(), req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{}`, thing(name))); res.StatusCode != 404 {
			t.Errorf("%s: status %d", name, res.StatusCode)
		}
	}
}

func TestSavingDefaultsReachesEveryPanelOfThatAccountAndNoOther(t *testing.T) {
	h, _, pub, acc := settingsHandler(t)
	ctx := context.Background()
	res, _ := h.Handle(ctx, req("PUT", "PUT /api/settings", "sub-a", `{"countdownLeadMin":360}`, nil))
	if res.StatusCode != 200 {
		t.Fatalf("status %d: %s", res.StatusCode, res.Body)
	}
	topics := map[string]string{}
	for _, m := range pub.Messages {
		topics[m.Topic] = string(m.Payload)
	}
	if len(topics) != 2 || topics["scoreboard/scoreboard-7qf2/config"] == "" || topics["scoreboard/scoreboard-aaaa/config"] == "" {
		t.Fatalf("published to %v, want sub-a's two panels and nothing else", topics)
	}
	for topic, payload := range topics {
		if !strings.Contains(payload, `"countdownLeadMin":360`) {
			t.Errorf("%s did not get the new default: %s", topic, payload)
		}
	}
	if d, _ := acc.Defaults(ctx, "sub-a"); d.CountdownLeadMin == nil || *d.CountdownLeadMin != 360 {
		t.Errorf("defaults not saved: %+v", d)
	}
	if d, _ := acc.Defaults(ctx, "sub-b"); d.CountdownLeadMin != nil {
		t.Error("another account's defaults changed")
	}
}

func TestAPanelsOwnValueBeatsTheAccountsDefault(t *testing.T) {
	h, _, pub, _ := settingsHandler(t)
	ctx := context.Background()
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"countdownLeadMin":60}`, thing("scoreboard-7qf2")))
	pub.Messages = nil
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/settings", "sub-a", `{"countdownLeadMin":360,"finalHoldMin":10}`, nil))
	for _, m := range pub.Messages {
		got := string(m.Payload)
		if m.Topic == "scoreboard/scoreboard-7qf2/config" && !(strings.Contains(got, `"countdownLeadMin":60`) && strings.Contains(got, `"finalHoldMin":10`)) {
			t.Errorf("the panel's own lead should win and the default hold show through: %s", got)
		}
	}
}

func TestOnePanelThatCannotBeReachedDoesNotStopTheOthers(t *testing.T) {
	h, _, pub, acc := settingsHandler(t)
	pub.FailTopics = map[string]bool{"scoreboard/scoreboard-7qf2/config": true}
	res, _ := h.Handle(context.Background(), req("PUT", "PUT /api/settings", "sub-a", `{"finalHoldMin":10}`, nil))
	if res.StatusCode != 200 {
		t.Fatalf("status %d: %s", res.StatusCode, res.Body)
	}
	var body struct {
		NotSent []string `json:"notSent"`
	}
	_ = json.Unmarshal([]byte(res.Body), &body)
	if len(body.NotSent) != 1 || body.NotSent[0] != "scoreboard-7qf2" {
		t.Errorf("notSent = %v", body.NotSent)
	}
	if d, _ := acc.Defaults(context.Background(), "sub-a"); d.FinalHoldMin == nil {
		t.Error("defaults were not saved; saving again could not converge")
	}
	sent := false
	for _, m := range pub.Messages {
		sent = sent || m.Topic == "scoreboard/scoreboard-aaaa/config"
	}
	if !sent {
		t.Error("the panel that could be reached was not sent its settings")
	}
}

func TestAPanelIsNotSentSettingsTheServerCouldNotRead(t *testing.T) {
	// If the account's defaults cannot be read, what would be published is
	// the built-in values: a panel silently losing its owner's sleep hours.
	h, _, pub, acc := settingsHandler(t)
	acc.Err = errors.New("throttled")
	for _, r := range []struct{ key, body string }{
		{"PUT /api/devices/{thing}/display", `{"finalHoldMin":30}`},
		{"PUT /api/devices/{thing}/game", `{"gameId":2026020001}`},
	} {
		res, _ := h.Handle(context.Background(), req("PUT", r.key, "sub-a", r.body, thing("scoreboard-7qf2")))
		if res.StatusCode != 500 {
			t.Errorf("%s: status %d, want 500", r.key, res.StatusCode)
		}
	}
	if len(pub.Messages) != 0 {
		t.Errorf("published %d documents built on settings nobody could read", len(pub.Messages))
	}
}

func TestReadingSettings(t *testing.T) {
	h, _, _, _ := settingsHandler(t)
	ctx := context.Background()
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/settings", "sub-a", `{"countdownLeadMin":360}`, nil))
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"finalHoldMin":30}`, thing("scoreboard-7qf2")))

	res, _ := h.Handle(ctx, req("GET", "GET /api/settings", "sub-a", "", nil))
	if !strings.Contains(res.Body, `"defaults":{"countdownLeadMin":360}`) || !strings.Contains(res.Body, `"builtIn":{"v":1,"countdownLeadMin":720,"finalHoldMin":180}`) {
		t.Errorf("settings = %s", res.Body)
	}
	res, _ = h.Handle(ctx, req("GET", "GET /api/devices", "sub-a", "", nil))
	for _, want := range []string{`"overrides":{"finalHoldMin":30}`, `"resolved":{"v":1,"countdownLeadMin":360,"finalHoldMin":30}`,
		`"sources":{"countdownLeadMin":"account","finalHoldMin":"panel","sleep":"built-in"}`} {
		if !strings.Contains(res.Body, want) {
			t.Errorf("list lacks %s:\n%s", want, res.Body)
		}
	}
	// Another account sees its own, which is nothing.
	res, _ = h.Handle(ctx, req("GET", "GET /api/settings", "sub-b", "", nil))
	if !strings.Contains(res.Body, `"defaults":{}`) {
		t.Errorf("sub-b sees %s", res.Body)
	}
}

func TestTheListStillAnswersWhenDefaultsCannotBeRead(t *testing.T) {
	h, _, _, acc := settingsHandler(t)
	acc.Err = errors.New("throttled")
	res, _ := h.Handle(context.Background(), req("GET", "GET /api/devices", "sub-a", "", nil))
	if res.StatusCode != 200 || !strings.Contains(res.Body, "scoreboard-7qf2") {
		t.Fatalf("status %d: %s", res.StatusCode, res.Body)
	}
	// It says less rather than something untrue: no resolved settings.
	if strings.Contains(res.Body, `"resolved"`) {
		t.Errorf("the list claims resolved settings it could not work out: %s", res.Body)
	}
}

// --- orientation (SCO-34): a panel's own, sent in the same document

func TestOrientationIsSavedSentAndClearedOnRelease(t *testing.T) {
	h, st, pub, _ := settingsHandler(t)
	ctx := context.Background()
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`, thing("scoreboard-7qf2")))
	stamp := clock.UnixMilli()
	clock = clock.Add(60 * 1e9)
	defer func() { clock = clock.Add(-60 * 1e9) }()

	res, _ := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"rotate":270}`, thing("scoreboard-7qf2")))
	if res.StatusCode != 200 || !strings.Contains(res.Body, `"overrides":{"rotate":270}`) || !strings.Contains(res.Body, `"finalHoldMin":180,"rotate":270}`) {
		t.Fatalf("status %d: %s", res.StatusCode, res.Body)
	}
	got := string(pub.Messages[len(pub.Messages)-1].Payload)
	// Turning a panel is not choosing a game: the stamp is re-sent unchanged.
	if !strings.Contains(got, `"rotate":270`) || !strings.Contains(got, `"chosenAt":`+strconv.FormatInt(stamp, 10)) {
		t.Errorf("document = %s", got)
	}
	// "auto" is stored as nothing, and the document then says nothing, so
	// the panel goes back to deciding for itself.
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"rotate":"auto"}`, thing("scoreboard-7qf2")))
	if got := string(pub.Messages[len(pub.Messages)-1].Payload); strings.Contains(got, "rotate") {
		t.Errorf("auto reached the wire: %s", got)
	}
	// Released, the panel forgets it along with the rest of its settings;
	// the next owner's glass may hang the other way.
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"rotate":180}`, thing("scoreboard-7qf2")))
	if res, _ := h.Handle(ctx, req("DELETE", "DELETE /api/devices/{thing}", "sub-a", "", thing("scoreboard-7qf2"))); res.StatusCode != 200 {
		t.Fatalf("release: status %d", res.StatusCode)
	}
	if d, _, _ := st.Get(ctx, "scoreboard-7qf2"); d.Display.Rotate != nil {
		t.Errorf("released, the panel still carries rotate %d", *d.Display.Rotate)
	}
}

func TestOrientationIsOneOfFiveWordsAndNeverAnAccountDefault(t *testing.T) {
	h, _, pub, _ := settingsHandler(t)
	ctx := context.Background()
	for _, body := range []string{`{"rotate":45}`, `{"rotate":"270"}`, `{"rotate":true}`, `{"rotate":"upside down"}`, `{"rotate":90.5}`} {
		if res, _ := h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", body, thing("scoreboard-7qf2"))); res.StatusCode != 400 {
			t.Errorf("%s: status %d", body, res.StatusCode)
		}
	}
	// Which way up a panel hangs is a fact about one piece of glass, not a
	// default; the account route refuses even a good value, and "auto".
	for _, body := range []string{`{"rotate":270}`, `{"rotate":"auto"}`, `{"finalHoldMin":30,"rotate":0}`} {
		if res, _ := h.Handle(ctx, req("PUT", "PUT /api/settings", "sub-a", body, nil)); res.StatusCode != 400 {
			t.Errorf("account route took %s: status %d", body, res.StatusCode)
		}
	}
	if len(pub.Messages) != 0 {
		t.Errorf("a refused request published %d messages", len(pub.Messages))
	}
}
