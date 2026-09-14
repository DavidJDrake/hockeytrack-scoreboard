package main

import (
	"context"
	"strings"
	"sync"
	"time"

	"hockeytrack-scoreboard/internal/enroll"
)

// Allowlist is the set of invited addresses: one comma-separated SSM
// parameter, read at most once per TTL by each warm Lambda instance, so a
// burst of sign-ins is not a burst of reads. The TTL is also the longest a
// removal waits to take effect.
type Allowlist struct {
	Fetch func(ctx context.Context) (string, error)
	TTL   time.Duration
	Now   func() time.Time

	mu      sync.Mutex
	members map[string]bool
	fetched time.Time
}

// Contains reports whether email is invited. Addresses compare the way the
// enroll flow compares an owner hint -- case and surrounding whitespace only --
// so an invitation and an ownership check never disagree about whether two
// spellings are the same person.
func (a *Allowlist) Contains(ctx context.Context, email string) (bool, error) {
	email = enroll.NormalizeOwner(email)

	a.mu.Lock()
	defer a.mu.Unlock()

	now := a.Now()
	if a.members == nil || now.Sub(a.fetched) >= a.TTL || now.Before(a.fetched) {
		raw, err := a.Fetch(ctx)
		if err != nil {
			// No falling back to the list already held. It is stale by
			// definition here, and serving it would keep admitting someone
			// just removed for as long as SSM stayed unreachable.
			a.members = nil
			return false, err
		}
		a.members = parseAllowlist(raw)
		a.fetched = now
	}
	return email != "" && a.members[email], nil
}

func parseAllowlist(raw string) map[string]bool {
	members := map[string]bool{}
	for _, entry := range strings.Split(raw, ",") {
		if e := enroll.NormalizeOwner(entry); e != "" {
			members[e] = true
		}
	}
	return members
}
