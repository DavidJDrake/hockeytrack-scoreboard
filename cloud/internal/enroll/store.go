package enroll

import (
	"context"
	"errors"
	"sync"
)

// Status values for a pending enrollment.
const (
	StatusPending  = "pending"  // waiting for somebody to type the code
	StatusClaiming = "claiming" // an owner won the race; the certificate is being minted
	StatusReady    = "ready"    // the certificate is stored and the device may collect it
)

var (
	// ErrNotFound means no enrollment matched. Callers turn this into 404 --
	// never 403 -- so a wrong collection token and a nonexistent one look the
	// same from outside.
	ErrNotFound = errors.New("enroll: not found")

	// ErrAlreadyClaimed means somebody already claimed this enrollment.
	ErrAlreadyClaimed = errors.New("enroll: already claimed")

	// ErrCodeTaken means the generated code is already in use. The caller
	// generates another rather than storing a duplicate: uniqueness enforced
	// at write, not detected afterwards.
	ErrCodeTaken = errors.New("enroll: code already in use")
)

// Pending is one enrollment in flight. Both secrets are stored hashed: a dump
// of this table yields nobody a usable token and nobody a claimable code.
type Pending struct {
	TokenHash     string // sha256 of the collection token; the partition key
	CodeHash      string // sha256 of the current pairing code; its reservation item's key
	CodeExpiresAt int64  // epoch seconds; when the code rotates
	OwnerHintHash string // sha256 of the normalized owner hint, or empty
	CSR           string // the submitted PEM
	ThingName     string // assigned by the server, never taken from the CSR
	Status        string // StatusPending, StatusClaiming or StatusReady
	CertPEM       string // written only by SetCertificate
	Owner         string // the claiming Cognito subject
	ExpiresAt     int64  // epoch seconds; the DynamoDB TTL attribute
}

// Two lifetimes, deliberately different. The enrollment lives as long as
// ExpiresAt because a device should not have to generate a new keypair just
// because nobody was home. The code lives until CodeExpiresAt -- minutes --
// because it is displayed on a screen anybody in the room can read, and a
// photograph of it should stop being useful quickly.

// Store holds enrollments in flight. Claim is the only operation that may set
// an owner, and it must fail rather than overwrite one.
type Store interface {
	Create(ctx context.Context, p Pending) error
	ByCodeHash(ctx context.Context, codeHash string) (Pending, bool, error)
	ByTokenHash(ctx context.Context, tokenHash string) (Pending, bool, error)
	// RotateCode replaces the pairing code on an enrollment that is still
	// pending. Returns ErrCodeTaken if the new code is already in use, so the
	// caller can generate another.
	RotateCode(ctx context.Context, tokenHash, newCodeHash string, expiresAt int64) error
	// Reserve is the one-time guard: pending -> claiming, atomically. It runs
	// before the certificate is minted, so a lost race costs nothing.
	Reserve(ctx context.Context, tokenHash, owner string) error
	// SetCertificate completes the claim: claiming -> ready.
	SetCertificate(ctx context.Context, tokenHash, certPEM string) error
	Delete(ctx context.Context, tokenHash string) error
}

// Fake is an in-memory Store for tests, so nothing here needs AWS.
type Fake struct {
	mu   sync.Mutex
	rows map[string]Pending
}

func NewFake() *Fake { return &Fake{rows: map[string]Pending{}} }

func (f *Fake) Create(_ context.Context, p Pending) error {
	if p.ExpiresAt == 0 {
		return errors.New("enroll: an enrollment needs an expiry")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, existing := range f.rows {
		if existing.CodeHash == p.CodeHash {
			return ErrCodeTaken
		}
	}
	p.Status = StatusPending
	f.rows[p.TokenHash] = p
	return nil
}

func (f *Fake) RotateCode(_ context.Context, tokenHash, newCodeHash string, expiresAt int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	p, ok := f.rows[tokenHash]
	if !ok {
		return ErrNotFound
	}
	if p.Status != StatusPending {
		return ErrAlreadyClaimed
	}
	for hash, existing := range f.rows {
		if hash != tokenHash && existing.CodeHash == newCodeHash {
			return ErrCodeTaken
		}
	}
	p.CodeHash, p.CodeExpiresAt = newCodeHash, expiresAt
	f.rows[tokenHash] = p
	return nil
}

func (f *Fake) ByTokenHash(_ context.Context, tokenHash string) (Pending, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	p, ok := f.rows[tokenHash]
	return p, ok, nil
}

func (f *Fake) ByCodeHash(_ context.Context, codeHash string) (Pending, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, p := range f.rows {
		if p.CodeHash == codeHash {
			return p, true, nil
		}
	}
	return Pending{}, false, nil
}

func (f *Fake) Reserve(_ context.Context, tokenHash, owner string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	p, ok := f.rows[tokenHash]
	if !ok {
		return ErrNotFound
	}
	if p.Status != StatusPending {
		return ErrAlreadyClaimed
	}
	p.Status, p.Owner = StatusClaiming, owner
	f.rows[tokenHash] = p
	return nil
}

func (f *Fake) SetCertificate(_ context.Context, tokenHash, certPEM string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	p, ok := f.rows[tokenHash]
	if !ok {
		return ErrNotFound
	}
	p.Status, p.CertPEM = StatusReady, certPEM
	f.rows[tokenHash] = p
	return nil
}

func (f *Fake) Delete(_ context.Context, tokenHash string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.rows, tokenHash)
	return nil
}
