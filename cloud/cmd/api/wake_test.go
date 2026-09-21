package main

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func wake(h *Handler, sub, panel, body string) (int, string) {
	res, _ := h.Handle(context.Background(), req("PUT", "PUT /api/devices/{thing}/wake", sub, body, thing(panel)))
	return res.StatusCode, res.Body
}

type wakeDoc struct {
	GameID   *int64 `json:"gameId"`
	ChosenAt int64  `json:"chosenAt"`
	Display  struct {
		FinalHoldMin int `json:"finalHoldMin"`
		Wake         *struct {
			Mode  string `json:"mode"`
			Until int64  `json:"until"`
		} `json:"wake"`
	} `json:"display"`
}

func TestTheSwitchIsSentInTheWholeDocumentAndIsNotAChoiceOfGame(t *testing.T) {
	h, st, pub, _ := settingsHandler(t)
	ctx := context.Background()
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/game", "sub-a", `{"gameId":2026020001}`, thing("scoreboard-7qf2")))
	_, _ = h.Handle(ctx, req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"finalHoldMin":30,"sleep":{"enabled":true,"start":"00:00","end":"07:00","zone":"America/Toronto"}}`, thing("scoreboard-7qf2")))
	before, _, _ := st.Get(ctx, "scoreboard-7qf2")

	status, body := wake(h, "sub-a", "scoreboard-7qf2", `{"mode":"asleep"}`)
	if status != 200 {
		t.Fatalf("%d %s", status, body)
	}
	var doc wakeDoc
	m := pub.Messages[len(pub.Messages)-1]
	_ = json.Unmarshal(m.Payload, &doc)
	if m.Topic != "scoreboard/scoreboard-7qf2/config" || !m.Retain {
		t.Errorf("published to %q retain=%v", m.Topic, m.Retain)
	}
	if doc.GameID == nil || *doc.GameID != 2026020001 || doc.Display.FinalHoldMin != 30 {
		t.Errorf("the rest of the document was dropped: %s", m.Payload)
	}
	if doc.ChosenAt != before.ChosenAt {
		t.Errorf("chosenAt moved from %d to %d: a panel would read the switch as a choice of game", before.ChosenAt, doc.ChosenAt)
	}
	// The test clock is 2 p.m. in Toronto; the window ends at 7 a.m.
	wantUntil := time.Date(2026, 10, 2, 11, 0, 0, 0, time.UTC).UnixMilli()
	if doc.Display.Wake == nil || doc.Display.Wake.Mode != "asleep" || doc.Display.Wake.Until != wantUntil {
		t.Errorf("wake = %+v, want asleep until %d: %s", doc.Display.Wake, wantUntil, m.Payload)
	}
	after, _, _ := st.Get(ctx, "scoreboard-7qf2")
	if after.Wake == nil || after.Wake.Until != wantUntil {
		t.Errorf("stored %+v", after.Wake)
	}
	if !strings.Contains(body, `"wake":{"mode":"asleep"`) {
		t.Errorf("the site is not told: %s", body)
	}
}

func TestAutoTakesTheSwitchOff(t *testing.T) {
	h, st, pub, _ := settingsHandler(t)
	wake(h, "sub-a", "scoreboard-7qf2", `{"mode":"awake"}`)
	if status, body := wake(h, "sub-a", "scoreboard-7qf2", `{"mode":"auto"}`); status != 200 || strings.Contains(body, `"wake"`) {
		t.Fatalf("%d %s", status, body)
	}
	if strings.Contains(string(pub.Messages[len(pub.Messages)-1].Payload), "wake") {
		t.Errorf("the panel was not told: %s", pub.Messages[len(pub.Messages)-1].Payload)
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); d.Wake != nil {
		t.Errorf("stored %+v", d.Wake)
	}
}

func TestAClientCannotSayWhenTheSwitchEnds(t *testing.T) {
	h, st, _, _ := settingsHandler(t)
	for name, body := range map[string]string{
		"an end of its own":   `{"mode":"awake","until":99999999999999}`,
		"a mode nobody knows": `{"mode":"forever"}`,
		"no mode":             `{}`,
		"not JSON":            `awake`,
		"two documents":       `{"mode":"awake"}{"mode":"asleep"}`,
		"a very large body":   `{"mode":"` + strings.Repeat("a", 5000) + `"}`,
	} {
		if status, res := wake(h, "sub-a", "scoreboard-7qf2", body); status != 400 {
			t.Errorf("%s: %d %s", name, status, res)
		}
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); d.Wake != nil {
		t.Errorf("something was stored: %+v", d.Wake)
	}
}

func TestSomebodyElsesPanelCannotBeSwitched(t *testing.T) {
	h, st, pub, _ := settingsHandler(t)
	for _, panel := range []string{"scoreboard-bbbb", "scoreboard-cccc", "scoreboard-none"} {
		if status, _ := wake(h, "sub-a", panel, `{"mode":"asleep"}`); status != 404 {
			t.Errorf("%s: %d", panel, status)
		}
	}
	if d, _, _ := st.Get(context.Background(), "scoreboard-bbbb"); d.Wake != nil || len(pub.Messages) != 0 {
		t.Errorf("stored %+v, published %d", d.Wake, len(pub.Messages))
	}
}

func TestASwitchThatHasEndedIsNeitherSentNorShown(t *testing.T) {
	h, _, pub, _ := settingsHandler(t)
	wake(h, "sub-a", "scoreboard-7qf2", `{"mode":"awake"}`)
	clock = clock.Add(13 * time.Hour) // no sleep hours on this panel: twelve hours
	defer func() { clock = clock.Add(-13 * time.Hour) }()
	res, _ := h.Handle(context.Background(), req("PUT", "PUT /api/devices/{thing}/display", "sub-a", `{"finalHoldMin":30}`, thing("scoreboard-7qf2")))
	if res.StatusCode != 200 || strings.Contains(res.Body, `"wake"`) {
		t.Errorf("%d %s", res.StatusCode, res.Body)
	}
	if got := string(pub.Messages[len(pub.Messages)-1].Payload); strings.Contains(got, "wake") {
		t.Errorf("an ended switch was sent: %s", got)
	}
}

func TestReleasingAPanelClearsTheSwitch(t *testing.T) {
	h, st, _, _ := settingsHandler(t)
	wake(h, "sub-a", "scoreboard-7qf2", `{"mode":"asleep"}`)
	_, _ = h.Handle(context.Background(), req("DELETE", "DELETE /api/devices/{thing}", "sub-a", "", thing("scoreboard-7qf2")))
	if d, _, _ := st.Get(context.Background(), "scoreboard-7qf2"); d.Wake != nil {
		t.Errorf("the next owner gets a panel that is dark and does not know why: %+v", d.Wake)
	}
}
