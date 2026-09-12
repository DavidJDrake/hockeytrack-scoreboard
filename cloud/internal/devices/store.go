// Package devices records which person owns which panel, and what that panel
// is currently showing. A certificate proves a device is genuine; this is
// what says whose it is.
package devices

import (
	"context"
	"errors"
	"sync"
)

var (
	// ErrNotFound is returned for a thing this store has never seen.
	ErrNotFound = errors.New("devices: not found")
	// ErrAlreadyClaimed is returned when claiming a device that already has
	// an owner. Claiming is one-time; re-claiming needs an unbind first.
	ErrAlreadyClaimed = errors.New("devices: already claimed")
	// ErrNotOwner is returned when the caller is not the device's owner.
	ErrNotOwner = errors.New("devices: not the owner")
)

// Device is one panel and its ownership.
type Device struct {
	ThingName string // the AWS IoT thing name, e.g. "scoreboard-01"
	Owner     string // the Cognito subject; empty means unclaimed
	Name      string // a display name the owner chooses, e.g. "Living room"
	GameID    int64  // the game the panel is following; 0 means none
	Code      string // the pairing code shown on the panel
}

// Store persists Devices. Claim is the only operation that may bind an owner,
// and it must fail rather than overwrite an existing one.
type Store interface {
	// Get returns the device for thingName, or found=false if none exists.
	Get(ctx context.Context, thingName string) (Device, bool, error)
	// ByCode returns an unclaimed device matching code, or found=false if none
	// exists or if the device is already claimed. A claimed device's code stops
	// resolving; codes are one-time tokens for pairing only.
	ByCode(ctx context.Context, code string) (Device, bool, error)
	// ListByOwner returns all devices claimed by owner.
	ListByOwner(ctx context.Context, owner string) ([]Device, error)
	// Register provisions a new device with its pairing code. Registering the
	// same thingName twice is idempotent and does not overwrite the original code,
	// for provisioning workflows that may retry.
	Register(ctx context.Context, thingName, code string) error
	// Claim binds a device to an owner. A device must have been registered and
	// must be unclaimed (Owner empty). Returns ErrNotFound if the device does
	// not exist or ErrAlreadyClaimed if it already has an owner.
	Claim(ctx context.Context, thingName, owner string) error
	// Update changes the mutable fields (Name, GameID) of a claimed device.
	// Code, ThingName, and Owner are immutable; Update checks that d.Owner
	// matches the stored owner and returns ErrNotOwner if not.
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

func (f *Fake) ByCode(_ context.Context, code string) (Device, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, d := range f.items {
		if d.Code == code && d.Owner == "" {
			return d, true, nil
		}
	}
	return Device{}, false, nil
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

func (f *Fake) Register(_ context.Context, thingName, code string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.items[thingName]; ok {
		return nil // registering twice is not an error; provisioning may retry
	}
	f.items[thingName] = Device{ThingName: thingName, Code: code}
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
	// Only Name and GameID are mutable; Code and ThingName are immutable.
	d.Name, d.GameID = in.Name, in.GameID
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
	d.Owner, d.Name, d.GameID = "", "", 0
	f.items[thingName] = d
	return nil
}
