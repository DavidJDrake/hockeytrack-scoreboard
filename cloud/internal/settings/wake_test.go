package settings

import (
	"errors"
	"testing"
	"time"
)

var midnightToSeven = &Sleep{Enabled: true, Start: "00:00", End: "07:00", Zone: "America/Toronto"}

func at(s string) time.Time {
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestASwitchEndsWhenSleepHoursNextEnd(t *testing.T) {
	for _, tc := range []struct{ name, now, until string }{
		{"awake at 1 a.m. lasts the rest of the night", "2026-09-21T05:00:00Z", "2026-09-21T11:00:00Z"},
		{"asleep at 8 p.m. lasts until morning", "2026-09-22T00:00:00Z", "2026-09-22T11:00:00Z"},
		{"pressed at exactly 7 a.m. it is tomorrow's 7 a.m.", "2026-09-21T11:00:00Z", "2026-09-22T11:00:00Z"},
		{"across the night the clocks go back the end is still 7 a.m. on the wall", "2026-11-01T03:00:00Z", "2026-11-01T12:00:00Z"},
	} {
		w, err := NewWake(WakeAwake, at(tc.now), midnightToSeven)
		if err != nil || w == nil || w.Until != at(tc.until).UnixMilli() {
			t.Errorf("%s: %+v %v, want until %s", tc.name, w, err, tc.until)
		}
	}
}

func TestWithNoSleepHoursASwitchLastsTwelveHours(t *testing.T) {
	now := at("2026-09-21T05:00:00Z")
	for _, sleep := range []*Sleep{nil, {Enabled: false, End: "07:00", Zone: "America/Toronto"}, {Enabled: true, End: "7am", Zone: "America/Toronto"}, {Enabled: true, End: "07:00", Zone: "Mars/Olympus"}} {
		w, err := NewWake(WakeAsleep, now, sleep)
		if err != nil || w.Until != now.Add(12*time.Hour).UnixMilli() {
			t.Errorf("%+v: %+v %v", sleep, w, err)
		}
	}
}

func TestNoSwitchEverLastsLongerThanADay(t *testing.T) {
	now := at("2026-09-21T05:00:00Z")
	for h := 0; h < 24; h++ {
		sleep := &Sleep{Enabled: true, Start: "00:00", End: time.Date(0, 1, 1, h, 30, 0, 0, time.UTC).Format("15:04"), Zone: "Pacific/Kiritimati"}
		w, _ := NewWake(WakeAwake, now, sleep)
		if left := time.UnixMilli(w.Until).Sub(now); left <= 0 || left > WakeMax {
			t.Errorf("end %s: lasts %s", sleep.End, left)
		}
	}
}

func TestAutoIsNoSwitchAndAnythingElseIsRefused(t *testing.T) {
	if w, err := NewWake(WakeAuto, time.Now(), midnightToSeven); w != nil || err != nil {
		t.Errorf("%+v %v", w, err)
	}
	for _, mode := range []string{"", "AWAKE", "on", "awake ", "asleep\x00"} {
		if _, err := NewWake(mode, time.Now(), midnightToSeven); !errors.Is(err, ErrWakeMode) {
			t.Errorf("%q: %v", mode, err)
		}
	}
}

func TestASwitchIsLiveOnlyWhileItCouldBeOneOfOurs(t *testing.T) {
	now := at("2026-09-21T05:00:00Z")
	ms := func(d time.Duration) int64 { return now.Add(d).UnixMilli() }
	for name, tc := range map[string]struct {
		w    *Wake
		live bool
	}{
		"none":                  {nil, false},
		"in force":              {&Wake{WakeAwake, ms(time.Hour)}, true},
		"ended":                 {&Wake{WakeAwake, ms(-time.Second)}, false},
		"ends this instant":     {&Wake{WakeAsleep, ms(0)}, false},
		"an end a year away":    {&Wake{WakeAsleep, ms(365 * 24 * time.Hour)}, false},
		"a mode nobody defined": {&Wake{"forever", ms(time.Hour)}, false},
	} {
		if got := tc.w.Live(now) != nil; got != tc.live {
			t.Errorf("%s: live=%v", name, got)
		}
	}
}

func TestADamagedStoredSwitchIsNoSwitch(t *testing.T) {
	w := &Wake{WakeAsleep, 1789977600000}
	if got := LoadWake(StoredWake(w)); got == nil || *got != *w {
		t.Errorf("%+v", got)
	}
	for _, s := range []string{"", "{", `{"mode":"forever","until":5}`, `{"mode":"asleep"}`, `{"mode":"asleep","until":-1}`, `[]`} {
		if got := LoadWake(s); got != nil {
			t.Errorf("%q: %+v", s, got)
		}
	}
	if StoredWake(nil) != "" {
		t.Error("no switch is stored as nothing")
	}
}
