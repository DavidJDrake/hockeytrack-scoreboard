// Package devices records which person owns which panel, and what that panel
// is currently showing. A certificate proves a device is genuine; this is
// what says whose it is.
package devices

import (
	"context"
	"errors"
	"regexp"
	"sync"

	"hockeytrack-scoreboard/internal/schedule"
	"hockeytrack-scoreboard/internal/settings"
)

var (
	// ErrNotFound is returned for a thing this store has never seen.
	ErrNotFound = errors.New("devices: not found")
	// ErrAlreadyClaimed is returned when claiming a device that already has
	// an owner. Claiming is one-time; re-claiming needs an unbind first.
	ErrAlreadyClaimed = errors.New("devices: already claimed")
	// ErrNotOwner is returned when the caller is not the device's owner.
	ErrNotOwner = errors.New("devices: not the owner")
	// ErrInvalidThingName is returned by Register for a name that is empty
	// or contains a character outside validThingName's set.
	ErrInvalidThingName = errors.New("devices: invalid thing name")
)

// thingNameRE bounds what Register accepts as a thing name. AWS IoT itself
// is looser -- CreateThing's thingName pattern is `[a-zA-Z0-9:_-]+` -- but
// handler.go interpolates ThingName directly into the MQTT topic
// "scoreboard/<name>/config", and IAM's "*" in that topic's ARN matches "/"
// too. A name holding a stray "/" would publish to a topic nothing
// subscribes to, silently. This set is a strict subset of AWS's own -- it
// additionally excludes ':', which AWS allows but nothing here needs -- so
// every name Register accepts is also guaranteed valid to AWS.
var thingNameRE = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)

func validThingName(name string) bool {
	return thingNameRE.MatchString(name)
}

// Device is one panel and its ownership.
type Device struct {
	ThingName string // the AWS IoT thing name, e.g. "scoreboard-01"
	Owner     string // the Cognito subject; empty means unclaimed
	Name      string // a display name the owner chooses, e.g. "Living room"
	GameID    int64  // the game the panel is following; 0 means none
	// ChosenAt is when the owner last chose GameID, in milliseconds on the
	// server's clock; 0 means never. It is the stamp sent to the panel, kept
	// so the site can run the panel's rule (a choice buys five minutes on
	// screen and re-arms a final's hold) and so a later publish that is not
	// a choice can re-send the same stamp.
	ChosenAt int64
	// Display is what the owner set on this panel in particular; a field it
	// leaves unset shows the account's default through.
	Display settings.Settings
	// Schedule is the games the owner has asked this panel to show, and the
	// conflicts among them the owner has answered. Nothing acts on it until
	// the director exists; GameID is still what the panel follows.
	Schedule schedule.Panel
}

// Store persists Devices. Claim is the only operation that may bind an owner,
// and it must fail rather than overwrite an existing one.
type Store interface {
	// Get returns the device for thingName, or found=false if none exists.
	Get(ctx context.Context, thingName string) (Device, bool, error)
	// ListByOwner returns all devices claimed by owner.
	ListByOwner(ctx context.Context, owner string) ([]Device, error)
	// Register provisions a new device. Registering the same thingName twice
	// is idempotent, for provisioning workflows that may retry. Returns
	// ErrInvalidThingName if thingName is empty or contains a character
	// outside validThingName's set.
	Register(ctx context.Context, thingName string) error
	// Claim binds a device to an owner. A device must have been registered and
	// must be unclaimed (Owner empty). Returns ErrNotFound if the device does
	// not exist or ErrAlreadyClaimed if it already has an owner.
	Claim(ctx context.Context, thingName, owner string) error
	// Update changes the mutable fields (Name, GameID) of a claimed device.
	// ThingName and Owner are immutable; Update checks that d.Owner matches
	// the stored owner and returns ErrNotOwner if not.
	Update(ctx context.Context, d Device) error
	// Unbind removes the owner from a device, leaving it claimable again.
	// Returns ErrNotOwner if the caller is not the current owner.
	Unbind(ctx context.Context, thingName, owner string) error
}

// Fake is an in-memory Store for tests.
type Fake struct {
	mu    sync.Mutex
	items map[string]Device
}

// NewFake returns an empty Fake store.
func NewFake() *Fake { return &Fake{items: map[string]Device{}} }

func (f *Fake) Get(_ context.Context, thingName string) (Device, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, ok := f.items[thingName]
	return d, ok, nil
}

func (f *Fake) ListByOwner(_ context.Context, owner string) ([]Device, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []Device
	for _, d := range f.items {
		if d.Owner == owner {
			out = append(out, d)
		}
	}
	return out, nil
}

func (f *Fake) Register(_ context.Context, thingName string) error {
	if !validThingName(thingName) {
		return ErrInvalidThingName
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.items[thingName]; ok {
		return nil // registering twice is not an error; provisioning may retry
	}
	f.items[thingName] = Device{ThingName: thingName}
	return nil
}

func (f *Fake) Claim(_ context.Context, thingName, owner string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, ok := f.items[thingName]
	if !ok {
		return ErrNotFound
	}
	if d.Owner != "" {
		return ErrAlreadyClaimed
	}
	d.Owner = owner
	f.items[thingName] = d
	return nil
}

func (f *Fake) Update(_ context.Context, in Device) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, ok := f.items[in.ThingName]
	if !ok {
		return ErrNotFound
	}
	if d.Owner != in.Owner {
		return ErrNotOwner
	}
	// Only these are mutable; ThingName and Owner are not.
	d.Name, d.GameID, d.ChosenAt, d.Display, d.Schedule = in.Name, in.GameID, in.ChosenAt, in.Display, in.Schedule
	f.items[in.ThingName] = d
	return nil
}

func (f *Fake) Unbind(_ context.Context, thingName, owner string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	d, ok := f.items[thingName]
	if !ok {
		return ErrNotFound
	}
	if d.Owner != owner {
		return ErrNotOwner
	}
	// The next owner inherits nothing: not the name, the game, the stamp,
	// the settings, or which games the last owner liked to watch.
	d.Owner, d.Name, d.GameID, d.ChosenAt, d.Display, d.Schedule = "", "", 0, 0, settings.Settings{}, schedule.Panel{}
	f.items[thingName] = d
	return nil
}
