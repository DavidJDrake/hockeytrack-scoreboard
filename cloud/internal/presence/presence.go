// Package presence answers one question about a panel: when was it last on
// the broker. Nothing in this stack recorded that before SCO-33; the panel
// cannot publish anything, so its only trace of life is the broker's own
// connect and disconnect record, which AWS keeps in the fleet index once
// connectivity indexing is on (terraform/iot.tf).
//
// The index is read, never written, by anything here. That is the reason it
// was chosen over an IoT rule writing a last-seen stamp onto the devices row:
// terraform/sweep.tf carries the comparison.
package presence

import (
	"context"
	"time"
)

// Seen is what the broker last recorded for one panel.
type Seen struct {
	// Connected is whether the panel is on the broker right now, as far as
	// the index knows. A connected panel has been seen by definition,
	// whatever At says: At is the time of the connect, which may be long
	// ago for a panel that has simply stayed up.
	Connected bool
	// At is when the panel last connected or disconnected. Zero means the
	// broker has no record: the panel has never connected since indexing was
	// turned on, or the index has no entry for the name at all. A caller
	// must treat zero as "unknown", never as "long ago".
	At time.Time
}

// Source looks up several panels at once. A name the index has no entry for
// is simply absent from the result, not an error.
type Source interface {
	Lookup(ctx context.Context, things []string) (map[string]Seen, error)
}

// Fake is an in-memory Source for tests: the map is the index.
type Fake map[string]Seen

func (f Fake) Lookup(_ context.Context, things []string) (map[string]Seen, error) {
	out := map[string]Seen{}
	for _, name := range things {
		if s, ok := f[name]; ok {
			out[name] = s
		}
	}
	return out, nil
}
