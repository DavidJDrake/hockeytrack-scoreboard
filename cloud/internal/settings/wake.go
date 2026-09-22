package settings

import (
	"encoding/json"
	"errors"
	"time"
)

// Wake is the owner overriding sleep hours by hand, for a while: "awake"
// keeps the panel lit through its sleep hours, "asleep" makes it dark now
// whatever the hour. It is not a setting and has no layers. It is a switch
// somebody pressed while looking at the panel, and it carries its own end so
// that a switch nobody remembers cannot leave a panel lit all night or dark
// all season.
type Wake struct {
	Mode  string `json:"mode"`
	Until int64  `json:"until"` // milliseconds since the epoch
}

const (
	WakeAwake  = "awake"
	WakeAsleep = "asleep"
	// WakeAuto is a request, never a stored value: follow sleep hours.
	WakeAuto = "auto"

	// wakeWithoutWindow is how long a switch lasts on a panel with no sleep
	// hours to end it.
	wakeWithoutWindow = 12 * time.Hour
	// WakeMax bounds every switch, and is the panel's bound too
	// (WAKE_MAX_S in main.py): a panel ignores an end further off than this.
	WakeMax = 24 * time.Hour
)

var ErrWakeMode = errors.New("settings: wake mode must be awake, asleep or auto")

// NewWake makes the switch for a mode pressed at now, or nil for auto. It
// ends when the panel's sleep hours next END -- "awake" at 1 a.m. lasts the
// rest of the night, "asleep" at 8 p.m. lasts until morning -- or after
// twelve hours on a panel with no sleep hours. The server works the end out;
// a client never supplies one.
func NewWake(mode string, now time.Time, sleep *Sleep) (*Wake, error) {
	switch mode {
	case WakeAuto:
		return nil, nil
	case WakeAwake, WakeAsleep:
	default:
		return nil, ErrWakeMode
	}
	until := now.Add(wakeWithoutWindow)
	if end, ok := nextEnd(now, sleep); ok {
		until = end
	}
	if until.Sub(now) > WakeMax {
		until = now.Add(WakeMax)
	}
	return &Wake{Mode: mode, Until: until.UnixMilli()}, nil
}

// nextEnd is the next moment, strictly after now, at which the wall clock in
// the window's zone reads its end time.
func nextEnd(now time.Time, sleep *Sleep) (time.Time, bool) {
	if sleep == nil || !sleep.Enabled {
		return time.Time{}, false
	}
	loc, err := time.LoadLocation(sleep.Zone)
	if err != nil {
		return time.Time{}, false
	}
	end, err := time.Parse("15:04", sleep.End)
	if err != nil {
		return time.Time{}, false
	}
	local := now.In(loc)
	for day := 0; day <= 2; day++ {
		// time.Date normalises a wall time a clock change skips; that is
		// within the hour either way, which is all a switch needs.
		at := time.Date(local.Year(), local.Month(), local.Day()+day, end.Hour(), end.Minute(), 0, 0, loc)
		if at.After(now) {
			return at, true
		}
	}
	return time.Time{}, false
}

// Live returns w if it is a switch still in force at now, and nil otherwise:
// nil, an unknown mode, an end that has passed, or an end further off than
// any switch this code makes could have.
func (w *Wake) Live(now time.Time) *Wake {
	if w == nil || (w.Mode != WakeAwake && w.Mode != WakeAsleep) {
		return nil
	}
	left := time.UnixMilli(w.Until).Sub(now)
	if left <= 0 || left > WakeMax {
		return nil
	}
	return w
}

// StoredWake and LoadWake are the row's attribute. Anything unreadable is no
// switch: a damaged attribute must not pin a panel dark.
func StoredWake(w *Wake) string {
	if w == nil {
		return ""
	}
	b, err := json.Marshal(w)
	if err != nil {
		return ""
	}
	return string(b)
}

func LoadWake(stored string) *Wake {
	if stored == "" {
		return nil
	}
	var w Wake
	if err := json.Unmarshal([]byte(stored), &w); err != nil || (w.Mode != WakeAwake && w.Mode != WakeAsleep) || w.Until <= 0 {
		return nil
	}
	return &w
}
