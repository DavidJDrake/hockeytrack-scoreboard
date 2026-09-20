package settings

import (
	"encoding/json"
	"errors"
	"os"
	"regexp"
	"strings"
	"testing"
)

func p(n int) *int { return &n }

var toronto = &Sleep{Enabled: true, Start: "23:00", End: "07:00", Zone: "America/Toronto"}

func TestNothingSetResolvesToTheBuiltInValues(t *testing.T) {
	r, src := Resolve(Settings{}, Settings{})
	if r.CountdownLeadMin != 720 || r.FinalHoldMin != 180 || r.Sleep != nil {
		t.Errorf("resolved = %+v", r)
	}
	if src != (Sources{FromBuiltIn, FromBuiltIn, FromBuiltIn}) {
		t.Errorf("sources = %+v", src)
	}
}

func TestLayersResolveFieldByField(t *testing.T) {
	account := Settings{CountdownLeadMin: p(360), Sleep: toronto}
	panel := Settings{FinalHoldMin: p(30)}
	r, src := Resolve(account, panel)
	if r.CountdownLeadMin != 360 || r.FinalHoldMin != 30 || r.Sleep == nil || r.Sleep.Zone != "America/Toronto" {
		t.Errorf("resolved = %+v", r)
	}
	if src != (Sources{FromAccount, FromPanel, FromAccount}) {
		t.Errorf("sources = %+v", src)
	}
}

func TestAPanelCanSwitchOffSleepHoursItsAccountHas(t *testing.T) {
	r, src := Resolve(Settings{Sleep: toronto}, Settings{Sleep: &Sleep{Enabled: false}})
	if r.Sleep != nil || src.Sleep != FromPanel {
		t.Errorf("sleep = %+v from %s, want none from the panel", r.Sleep, src.Sleep)
	}
}

func TestZeroIsAValueNotAnAbsence(t *testing.T) {
	r, src := Resolve(Settings{CountdownLeadMin: p(360)}, Settings{CountdownLeadMin: p(0)})
	if r.CountdownLeadMin != 0 || src.CountdownLeadMin != FromPanel {
		t.Errorf("lead = %d from %s", r.CountdownLeadMin, src.CountdownLeadMin)
	}
}

func TestResolveDoesNotShareTheCallersSleep(t *testing.T) {
	mine := *toronto
	r, _ := Resolve(Settings{Sleep: &mine}, Settings{})
	r.Sleep.Zone = "changed"
	if mine.Zone != "America/Toronto" {
		t.Error("resolving handed back the caller's own struct")
	}
}

func TestBoundsAreInclusive(t *testing.T) {
	for _, ok := range []Settings{
		{CountdownLeadMin: p(0)}, {CountdownLeadMin: p(CountdownLeadMaxMin)},
		{FinalHoldMin: p(0)}, {FinalHoldMin: p(FinalHoldMaxMin)},
	} {
		if _, err := Normalize(ok); err != nil {
			t.Errorf("%+v rejected: %v", ok, err)
		}
	}
	for _, bad := range []Settings{
		{CountdownLeadMin: p(-1)}, {CountdownLeadMin: p(CountdownLeadMaxMin + 1)},
		{FinalHoldMin: p(-1)}, {FinalHoldMin: p(FinalHoldMaxMin + 1)},
	} {
		if _, err := Normalize(bad); !errors.Is(err, ErrInvalid) {
			t.Errorf("%+v accepted (err %v)", bad, err)
		}
	}
}

func TestSleepWindowsThatAreRefused(t *testing.T) {
	for name, s := range map[string]Sleep{
		"not a time":            {Enabled: true, Start: "11pm", End: "07:00", Zone: "America/Toronto"},
		"hour 24":               {Enabled: true, Start: "24:00", End: "07:00", Zone: "America/Toronto"},
		"minute 60":             {Enabled: true, Start: "23:00", End: "07:60", Zone: "America/Toronto"},
		"one digit":             {Enabled: true, Start: "7:00", End: "09:00", Zone: "America/Toronto"},
		"no zone":               {Enabled: true, Start: "23:00", End: "07:00"},
		"unknown zone":          {Enabled: true, Start: "23:00", End: "07:00", Zone: "Mars/Olympus"},
		"Local":                 {Enabled: true, Start: "23:00", End: "07:00", Zone: "Local"},
		"leaves the directory":  {Enabled: true, Start: "23:00", End: "07:00", Zone: "../../etc/passwd"},
		"absolute path":         {Enabled: true, Start: "23:00", End: "07:00", Zone: "/etc/localtime"},
		"a newline in the name": {Enabled: true, Start: "23:00", End: "07:00", Zone: "America/Toronto\nX"},
		"far too long":          {Enabled: true, Start: "23:00", End: "07:00", Zone: "A/" + strings.Repeat("b", 80)},
	} {
		if _, err := Normalize(Settings{Sleep: &s}); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: accepted (err %v)", name, err)
		}
	}
}

func TestZonesPeopleActuallyChoose(t *testing.T) {
	for _, zone := range []string{"America/Toronto", "America/Argentina/Buenos_Aires", "Europe/London", "UTC", "Etc/GMT+5", "Australia/Lord_Howe"} {
		s := Sleep{Enabled: true, Start: "23:00", End: "07:00", Zone: zone}
		if _, err := Normalize(Settings{Sleep: &s}); err != nil {
			t.Errorf("%s rejected: %v", zone, err)
		}
	}
}

func TestAWindowWithEqualEndsIsStoredAsSwitchedOff(t *testing.T) {
	n, err := Normalize(Settings{Sleep: &Sleep{Enabled: true, Start: "09:00", End: "09:00", Zone: "America/Toronto"}})
	if err != nil || n.Sleep == nil || n.Sleep.Enabled || n.Sleep.Zone != "" {
		t.Errorf("stored as %+v (err %v)", n.Sleep, err)
	}
}

func TestSwitchedOffSleepKeepsNoLeftovers(t *testing.T) {
	n, _ := Normalize(Settings{Sleep: &Sleep{Enabled: false, Start: "junk", Zone: "../x"}})
	if *n.Sleep != (Sleep{}) {
		t.Errorf("stored %+v; a switched-off window should carry nothing", *n.Sleep)
	}
}

func TestTheWireBlockIsBuiltFromTypedFieldsOnly(t *testing.T) {
	r, _ := Resolve(Settings{Sleep: toronto}, Settings{CountdownLeadMin: p(120)})
	b, _ := json.Marshal(r.Wire())
	want := `{"v":1,"countdownLeadMin":120,"finalHoldMin":180,"sleep":{"start":"23:00","end":"07:00","zone":"America/Toronto"}}`
	if string(b) != want {
		t.Errorf("wire = %s\nwant   %s", b, want)
	}
	none, _ := Resolve(Settings{}, Settings{})
	b, _ = json.Marshal(none.Wire())
	if string(b) != `{"v":1,"countdownLeadMin":720,"finalHoldMin":180}` {
		t.Errorf("wire with no sleep = %s", b)
	}
}

func TestDecodeIsStrict(t *testing.T) {
	for name, body := range map[string]string{
		"unknown key":      `{"countdownLeadMins":60}`,
		"a smuggled game":  `{"gameId":5,"countdownLeadMin":60}`,
		"unknown in sleep": `{"sleep":{"enabled":true,"start":"23:00","end":"07:00","zone":"UTC","x":1}}`,
		"a string number":  `{"countdownLeadMin":"60"}`,
		"a fraction":       `{"countdownLeadMin":1.5}`,
		"true":             `{"countdownLeadMin":true}`,
		"an array":         `[]`,
		"two documents":    `{}{}`,
		"not json":         `lead=60`,
		"empty":            ``,
	} {
		if _, err := Decode([]byte(body)); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: accepted (err %v)", name, err)
		}
	}
	s, err := Decode([]byte(`{"finalHoldMin":30}`))
	if err != nil || s.FinalHoldMin == nil || *s.FinalHoldMin != 30 || s.CountdownLeadMin != nil || s.Sleep != nil {
		t.Errorf("decoded %+v (err %v)", s, err)
	}
	if s, err := Decode([]byte(`{}`)); err != nil || s != (Settings{}) {
		t.Errorf("an empty layer: %+v, %v", s, err)
	}
}

func TestStoredRoundTripsAndDamageReadsAsNothing(t *testing.T) {
	in := Settings{CountdownLeadMin: p(0), Sleep: toronto}
	out := Load(Stored(in))
	if out.CountdownLeadMin == nil || *out.CountdownLeadMin != 0 || out.Sleep == nil || *out.Sleep != *toronto {
		t.Errorf("round trip: %+v", out)
	}
	for _, damaged := range []string{"", "not json", `{"countdownLeadMin":99999}`, `{"sleep":{"enabled":true,"zone":"../x","start":"23:00","end":"07:00"}}`} {
		if got := Load(damaged); got != (Settings{}) {
			t.Errorf("Load(%q) = %+v, want a layer that says nothing", damaged, got)
		}
	}
}

// The panel checks what it is sent to the same bounds. Two copies of a
// number drift unless something reads both.
func TestTheBoundsAndDefaultsAreThePanels(t *testing.T) {
	py, err := os.ReadFile("../../../device/scoreboard/main.py")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		`COUNTDOWN_LEAD_MAX_MIN = 48 \* 60`, `FINAL_HOLD_MAX_MIN = 24 \* 60`, `DISPLAY_FORMAT = 1`,
		`countdown_lead_s: int = 12 \* 60 \* 60`, `final_hold_s: int = 3 \* 60 \* 60`,
	} {
		if !regexp.MustCompile(want).Match(py) {
			t.Errorf("main.py no longer says %s", want)
		}
	}
	if CountdownLeadMaxMin != 48*60 || FinalHoldMaxMin != 24*60 || wireFormat != 1 || BuiltIn.CountdownLeadMin != 720 || BuiltIn.FinalHoldMin != 180 {
		t.Error("this package's numbers have moved away from the panel's")
	}
}
