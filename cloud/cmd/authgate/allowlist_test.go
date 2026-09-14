package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

// clock is a time source a test can move, in either direction.
type clock struct{ t time.Time }

func (c *clock) now() time.Time          { return c.t }
func (c *clock) advance(d time.Duration) { c.t = c.t.Add(d) }

// source stands in for the SSM parameter: a test can edit it, break it, and
// count how often it is read.
type source struct {
	value string
	err   error
	reads int
}

func (s *source) fetch(context.Context) (string, error) {
	s.reads++
	return s.value, s.err
}

func newAllowlist(src *source, c *clock) *Allowlist {
	return &Allowlist{Fetch: src.fetch, TTL: time.Minute, Now: c.now}
}

func newClock() *clock { return &clock{t: time.Unix(1_800_000_000, 0)} }

func mustContain(t *testing.T, a *Allowlist, email string, want bool) {
	t.Helper()
	got, err := a.Contains(context.Background(), email)
	if err != nil {
		t.Fatalf("Contains(%q): %v", email, err)
	}
	if got != want {
		t.Fatalf("Contains(%q) = %v, want %v", email, got, want)
	}
}

func TestAllowlistReadsAtMostOncePerTTL(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	c.advance(59 * time.Second)
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 1 {
		t.Fatalf("reads within the TTL = %d, want 1", src.reads)
	}
	c.advance(time.Second)
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 2 {
		t.Fatalf("reads once the TTL has passed = %d, want 2", src.reads)
	}
}

func TestAllowlistRemovalTakesEffectOnceTheTTLPasses(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	src.value = "someone.else@example.com"
	c.advance(59 * time.Second)
	// The documented cost of the cache: up to a minute of grace.
	mustContain(t, a, "owner@example.com", true)
	c.advance(time.Second)
	mustContain(t, a, "owner@example.com", false)
}

func TestAllowlistFailsClosedRatherThanUsingAStaleList(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	c.advance(time.Minute)
	src.err = errors.New("ssm unavailable")
	got, err := a.Contains(context.Background(), "owner@example.com")
	if err == nil || got {
		t.Fatalf("Contains with SSM down = %v, %v; want false and an error", got, err)
	}

	// A failed read is not remembered as an answer: the next call tries again.
	src.err = nil
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 3 {
		t.Fatalf("reads = %d, want 3", src.reads)
	}
}

func TestAllowlistNormalizesCaseAndWhitespaceOnly(t *testing.T) {
	src := &source{value: " Owner@Example.com ,, second@example.com"}
	a := newAllowlist(src, newClock())

	mustContain(t, a, "OWNER@example.COM ", true)
	mustContain(t, a, "second@example.com", true)
	// Plus addressing and dots are meaningful to Cognito, so they are
	// different people here too.
	mustContain(t, a, "owner+x@example.com", false)
	mustContain(t, a, "o.wner@example.com", false)
	// The empty entries in the list must not admit an empty address.
	mustContain(t, a, "", false)
	mustContain(t, a, "   ", false)
}

func TestAllowlistRereadsWhenTheClockGoesBackwards(t *testing.T) {
	src := &source{value: "owner@example.com"}
	c := newClock()
	a := newAllowlist(src, c)

	mustContain(t, a, "owner@example.com", true)
	c.advance(-time.Second)
	mustContain(t, a, "owner@example.com", true)
	if src.reads != 2 {
		t.Fatalf("reads after the clock moved backwards = %d, want 2", src.reads)
	}
}
