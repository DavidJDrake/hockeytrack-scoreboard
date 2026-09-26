// Package settings holds a panel's display settings: what the owner may set,
// the bounds on it, how an account's defaults and a panel's overrides
// resolve, and the block that goes on the wire to the panel.
//
// Three layers, resolved here and nowhere else: the built-in values, the
// account's defaults, and the panel's overrides, field by field. The panel
// only ever receives the result. It checks that result again to the same
// bounds (device/scoreboard/main.py, parse_display): this package is the
// authority on what is stored, not something the panel trusts.
package settings

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"time"

	// The zone list travels with the binary. Lambda's runtime image is not
	// promised to carry one, and "the zone is valid where it was checked but
	// not where it is used" is the failure this avoids.
	_ "time/tzdata"
)

// The bounds. The panel holds the same ones (COUNTDOWN_LEAD_MAX_MIN,
// FINAL_HOLD_MAX_MIN in main.py); a test reads that file to keep them equal.
const (
	CountdownLeadMaxMin = 48 * 60
	FinalHoldMaxMin     = 24 * 60
	wireFormat          = 1
)

// BuiltIn is what a panel runs on when nobody has said anything, and it is
// the panel's own Display() defaults: twelve hours, three hours, no sleep.
var BuiltIn = Resolved{CountdownLeadMin: 720, FinalHoldMin: 180}

// Sleep is a daily window in local wall time in an IANA zone. Enabled false
// is a value, not an absence: "no sleep hours on this panel" has to be
// sayable over an account default that has some.
type Sleep struct {
	Enabled bool   `json:"enabled"`
	Start   string `json:"start,omitempty"`
	End     string `json:"end,omitempty"`
	Zone    string `json:"zone,omitempty"`
}

// Rotate is which way up a panel hangs: a quarter turn clockwise, one of 0,
// 90, 180 or 270, or RotateAuto, which is how a request says "auto" and is
// stored as nothing at all (Normalize). The panel then decides from the
// shape of its display, or from what its own card says. The set is closed
// because the panel's placement raises on anything else, and a value that
// could raise there must never reach the wire.
type Rotate int

const RotateAuto Rotate = -1

var rotations = map[Rotate]bool{0: true, 90: true, 180: true, 270: true}

func (r *Rotate) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err == nil {
		if s != "auto" {
			return invalid(`rotate must be 0, 90, 180, 270 or "auto"`)
		}
		*r = RotateAuto
		return nil
	}
	var n int
	if err := json.Unmarshal(b, &n); err != nil || !rotations[Rotate(n)] {
		return invalid(`rotate must be 0, 90, 180, 270 or "auto"`)
	}
	*r = Rotate(n)
	return nil
}

// Settings is one layer: an account's defaults or a panel's overrides. A nil
// field says nothing, and the layer beneath shows through.
type Settings struct {
	CountdownLeadMin *int   `json:"countdownLeadMin,omitempty"`
	FinalHoldMin     *int   `json:"finalHoldMin,omitempty"`
	Sleep            *Sleep `json:"sleep,omitempty"`
	// Rotate is a panel's alone: which way up it hangs is a fact about one
	// piece of glass, not a default an account could have. DecodeAccount
	// refuses it, and Resolve reads it from the panel layer only, so a value
	// stored on an account by some other path would still never reach a
	// panel.
	Rotate *Rotate `json:"rotate,omitempty"`
}

// Resolved is what a panel actually runs on. Sleep nil means none; Rotate
// nil means the panel decides.
type Resolved struct {
	CountdownLeadMin int
	FinalHoldMin     int
	Sleep            *Sleep
	Rotate           *Rotate
}

// Source names the layer a resolved field came from, for the site to say so.
type Source string

const (
	FromBuiltIn Source = "built-in"
	FromAccount Source = "account"
	FromPanel   Source = "panel"
)

type Sources struct {
	CountdownLeadMin Source `json:"countdownLeadMin"`
	FinalHoldMin     Source `json:"finalHoldMin"`
	Sleep            Source `json:"sleep"`
}

var (
	hhmm = regexp.MustCompile(`^([01][0-9]|2[0-3]):[0-5][0-9]$`)
	// An IANA name's alphabet, checked before the name goes anywhere near
	// LoadLocation, which resolves names against a directory tree.
	zoneName = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+){0,2}$`)
)

// ErrInvalid wraps every validation failure, so a handler can tell a bad
// request from a broken server without reading the message.
var ErrInvalid = errors.New("invalid settings")

func invalid(format string, a ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, a...))
}

// Normalize validates one layer and returns it in the form it is stored in.
// The only rewriting it does: a sleep window whose ends are equal is no
// window, and is stored as one that is switched off.
func Normalize(s Settings) (Settings, error) {
	if s.CountdownLeadMin != nil && (*s.CountdownLeadMin < 0 || *s.CountdownLeadMin > CountdownLeadMaxMin) {
		return Settings{}, invalid("countdownLeadMin must be 0 to %d", CountdownLeadMaxMin)
	}
	if s.FinalHoldMin != nil && (*s.FinalHoldMin < 0 || *s.FinalHoldMin > FinalHoldMaxMin) {
		return Settings{}, invalid("finalHoldMin must be 0 to %d", FinalHoldMaxMin)
	}
	if s.Rotate != nil {
		// A stored row is decoded through UnmarshalJSON too, so this is only
		// reached with one of the five; "auto" is stored as nothing.
		if !rotations[*s.Rotate] && *s.Rotate != RotateAuto {
			return Settings{}, invalid(`rotate must be 0, 90, 180, 270 or "auto"`)
		}
		if *s.Rotate == RotateAuto {
			s.Rotate = nil
		}
	}
	if s.Sleep == nil {
		return s, nil
	}
	if !s.Sleep.Enabled {
		s.Sleep = &Sleep{}
		return s, nil
	}
	if !hhmm.MatchString(s.Sleep.Start) || !hhmm.MatchString(s.Sleep.End) {
		return Settings{}, invalid("sleep start and end must be HH:MM")
	}
	if err := checkZone(s.Sleep.Zone); err != nil {
		return Settings{}, err
	}
	if s.Sleep.Start == s.Sleep.End {
		s.Sleep = &Sleep{}
		return s, nil
	}
	sl := *s.Sleep
	s.Sleep = &sl
	return s, nil
}

func checkZone(name string) error {
	// "Local" and "" both load, and both mean "wherever this Lambda runs".
	if name == "" || name == "Local" || len(name) > 64 || !zoneName.MatchString(name) {
		return invalid("sleep zone must be an IANA time zone name")
	}
	if _, err := time.LoadLocation(name); err != nil {
		return invalid("sleep zone is not a time zone this service knows")
	}
	return nil
}

// Resolve lays a panel's overrides over an account's defaults over the
// built-in values, field by field.
func Resolve(account, panel Settings) (Resolved, Sources) {
	r, src := BuiltIn, Sources{FromBuiltIn, FromBuiltIn, FromBuiltIn}
	for _, layer := range []struct {
		s    Settings
		from Source
	}{{account, FromAccount}, {panel, FromPanel}} {
		if layer.s.CountdownLeadMin != nil {
			r.CountdownLeadMin, src.CountdownLeadMin = *layer.s.CountdownLeadMin, layer.from
		}
		if layer.s.FinalHoldMin != nil {
			r.FinalHoldMin, src.FinalHoldMin = *layer.s.FinalHoldMin, layer.from
		}
		if layer.s.Sleep != nil {
			src.Sleep = layer.from
			if layer.s.Sleep.Enabled {
				sl := *layer.s.Sleep
				r.Sleep = &sl
			} else {
				r.Sleep = nil
			}
		}
		// Orientation is the panel's own (see Settings.Rotate): an account
		// layer that somehow carries one is not looked at.
		if layer.from == FromPanel && layer.s.Rotate != nil {
			rot := *layer.s.Rotate
			r.Rotate = &rot
		}
	}
	return r, src
}

type wireSleep struct {
	Start string `json:"start"`
	End   string `json:"end"`
	Zone  string `json:"zone"`
}

// Wire is the "display" block of a panel's config document. Built from typed
// fields and nothing else: no client JSON is ever forwarded to a panel.
type Wire struct {
	V                int        `json:"v"`
	CountdownLeadMin int        `json:"countdownLeadMin"`
	FinalHoldMin     int        `json:"finalHoldMin"`
	Sleep            *wireSleep `json:"sleep,omitempty"`
	// Wake is the owner's hand on the switch, while it lasts. Not part of
	// Resolved: it is not a setting, and panelconfig adds it.
	Wake *Wake `json:"wake,omitempty"`
	// Rotate is carried only when the owner set one. Absent, the panel
	// decides for itself, as it did before this key existed, so a document
	// from this build turns no panel that nobody asked to be turned.
	Rotate *Rotate `json:"rotate,omitempty"`
}

func (r Resolved) Wire() Wire {
	w := Wire{V: wireFormat, CountdownLeadMin: r.CountdownLeadMin, FinalHoldMin: r.FinalHoldMin, Rotate: r.Rotate}
	if r.Sleep != nil {
		w.Sleep = &wireSleep{r.Sleep.Start, r.Sleep.End, r.Sleep.Zone}
	}
	return w
}

// View is what the site is shown for a panel: what was set on it, what it
// runs on, and where each value came from.
type View struct {
	Overrides Settings `json:"overrides"`
	Resolved  Wire     `json:"resolved"`
	Sources   Sources  `json:"sources"`
}

func ViewOf(account, panel Settings) View {
	r, src := Resolve(account, panel)
	return View{Overrides: panel, Resolved: r.Wire(), Sources: src}
}

// Decode reads one layer from a request body, strictly: an unknown key is an
// error, so a typo cannot be stored as "nothing was set".
func Decode(body []byte) (Settings, error) {
	s, err := decode(body)
	if err != nil {
		return Settings{}, err
	}
	return Normalize(s)
}

// DecodeAccount is Decode for an account's defaults, where orientation has
// no meaning: which way up a panel hangs is a fact about one piece of
// glass. The key is refused before Normalize turns "auto" into nothing, so
// the account route cannot be used to say it at all.
func DecodeAccount(body []byte) (Settings, error) {
	s, err := decode(body)
	if err != nil {
		return Settings{}, err
	}
	if s.Rotate != nil {
		return Settings{}, invalid("rotate is set on a panel, not an account")
	}
	return Normalize(s)
}

func decode(body []byte) (Settings, error) {
	var s Settings
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&s); err != nil {
		return Settings{}, invalid("not a settings document")
	}
	if dec.More() {
		return Settings{}, invalid("not a settings document")
	}
	return s, nil
}

// Stored encodes a layer for a table attribute, and Load reads it back. An
// empty or unreadable attribute is a layer that says nothing: a row written
// before this existed, or damaged, must not take the panel list down.
func Stored(s Settings) string {
	b, _ := json.Marshal(s)
	return string(b)
}

func Load(attr string) Settings {
	if attr == "" {
		return Settings{}
	}
	var s Settings
	if err := json.Unmarshal([]byte(attr), &s); err != nil {
		return Settings{}
	}
	if n, err := Normalize(s); err == nil {
		return n
	}
	return Settings{}
}
