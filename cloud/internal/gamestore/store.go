// Package gamestore persists one reduce.State per game and lists the games
// still worth watching.
package gamestore

import (
	"context"
	"errors"
	"sync"

	"hockeytrack-scoreboard/internal/reduce"
)

// ErrConflict is returned by Put when the stored version does not match the
// version the caller last observed.
var ErrConflict = errors.New("gamestore: version conflict")

// Store persists one reduce.State per game with an optimistic lock on
// State.Version so concurrent reducer invocations converge instead of
// clobbering each other.
type Store interface {
	// Get returns the state for gameID, or found=false if none exists.
	Get(ctx context.Context, gameID int64) (reduce.State, bool, error)
	// Put writes s conditionally on s.Version matching the stored version
	// (0 meaning "does not exist yet"). On success the stored version is
	// incremented. On mismatch it returns ErrConflict.
	Put(ctx context.Context, s reduce.State) error
	// ListActive returns states whose GameState is not "FINAL", or that
	// were updated within the last 6 hours.
	ListActive(ctx context.Context) ([]reduce.State, error)
}

// Fake is an in-memory Store for tests.
type Fake struct {
	mu    sync.Mutex
	items map[int64]reduce.State
}

// NewFake returns an empty Fake store.
func NewFake() *Fake { return &Fake{items: map[int64]reduce.State{}} }

func (f *Fake) Get(_ context.Context, gameID int64) (reduce.State, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.items[gameID]
	return s, ok, nil
}

func (f *Fake) Put(_ context.Context, s reduce.State) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if cur, ok := f.items[s.GameID]; ok {
		if cur.Version != s.Version {
			return ErrConflict
		}
	} else if s.Version != 0 {
		return ErrConflict
	}
	s.Version++
	f.items[s.GameID] = s
	return nil
}

func (f *Fake) ListActive(_ context.Context) ([]reduce.State, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []reduce.State
	for _, s := range f.items {
		out = append(out, s)
	}
	return out, nil
}
