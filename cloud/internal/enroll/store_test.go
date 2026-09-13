package enroll

import (
	"context"
	"errors"
	"testing"
	"time"
)

func pending(t *testing.T) (Pending, string, string) {
	t.Helper()
	code, err := Code()
	if err != nil {
		t.Fatal(err)
	}
	token, err := Token()
	if err != nil {
		t.Fatal(err)
	}
	return Pending{
		TokenHash:     HashSecret(token),
		CodeHash:      HashSecret(code),
		CodeExpiresAt: time.Now().Add(15 * time.Minute).Unix(),
		CSR:           "-----BEGIN CERTIFICATE REQUEST-----\nx\n-----END CERTIFICATE REQUEST-----\n",
		ThingName:     "scoreboard-abc123def456",
		ExpiresAt:     time.Now().Add(24 * time.Hour).Unix(),
	}, code, token
}

func TestFakeRoundTripsByBothLookups(t *testing.T) {
	ctx, s := context.Background(), NewFake()
	p, code, token := pending(t)
	if err := s.Create(ctx, p); err != nil {
		t.Fatal(err)
	}
	byToken, found, err := s.ByTokenHash(ctx, HashSecret(token))
	if err != nil || !found {
		t.Fatalf("ByTokenHash found=%v err=%v", found, err)
	}
	if byToken.ThingName != p.ThingName {
		t.Errorf("ByTokenHash returned %q", byToken.ThingName)
	}
	byCode, found, err := s.ByCodeHash(ctx, HashSecret(code))
	if err != nil || !found {
		t.Fatalf("ByCodeHash found=%v err=%v", found, err)
	}
	if byCode.ThingName != p.ThingName {
		t.Errorf("ByCodeHash returned %q", byCode.ThingName)
	}
}

func TestUnknownLookupsAreNotFoundRatherThanErrors(t *testing.T) {
	ctx, s := context.Background(), NewFake()
	if _, found, err := s.ByTokenHash(ctx, HashSecret("nope")); found || err != nil {
		t.Errorf("ByTokenHash on an unknown token: found=%v err=%v", found, err)
	}
	if _, found, err := s.ByCodeHash(ctx, HashSecret("nope")); found || err != nil {
		t.Errorf("ByCodeHash on an unknown code: found=%v err=%v", found, err)
	}
}

func TestReserveThenSetCertificate(t *testing.T) {
	ctx, s := context.Background(), NewFake()
	p, _, token := pending(t)
	if err := s.Create(ctx, p); err != nil {
		t.Fatal(err)
	}
	if err := s.Reserve(ctx, p.TokenHash, "cognito-sub-1"); err != nil {
		t.Fatal(err)
	}
	mid, _, _ := s.ByTokenHash(ctx, p.TokenHash)
	if mid.Status != StatusClaiming || mid.Owner != "cognito-sub-1" {
		t.Fatalf("after Reserve: %+v", mid)
	}
	if err := s.SetCertificate(ctx, p.TokenHash, "CERTPEM"); err != nil {
		t.Fatal(err)
	}
	got, _, err := s.ByTokenHash(ctx, HashSecret(token))
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != StatusReady || got.CertPEM != "CERTPEM" {
		t.Errorf("after SetCertificate: %+v", got)
	}
}

func TestReservingTwiceFails(t *testing.T) {
	// The one-time property, and the reason Reserve exists separately from
	// SetCertificate: issuing a certificate calls AWS, so if the guard ran
	// after issuance two people racing the same code would both mint one
	// before either lost.
	ctx, s := context.Background(), NewFake()
	p, _, _ := pending(t)
	if err := s.Create(ctx, p); err != nil {
		t.Fatal(err)
	}
	if err := s.Reserve(ctx, p.TokenHash, "first"); err != nil {
		t.Fatal(err)
	}
	if err := s.Reserve(ctx, p.TokenHash, "second"); !errors.Is(err, ErrAlreadyClaimed) {
		t.Fatalf("second Reserve returned %v, want ErrAlreadyClaimed", err)
	}
	got, _, _ := s.ByTokenHash(ctx, p.TokenHash)
	if got.Owner != "first" {
		t.Errorf("owner became %q", got.Owner)
	}
}

func TestReservingSomethingAlreadyReadyFails(t *testing.T) {
	ctx, s := context.Background(), NewFake()
	p, _, _ := pending(t)
	_ = s.Create(ctx, p)
	_ = s.Reserve(ctx, p.TokenHash, "first")
	_ = s.SetCertificate(ctx, p.TokenHash, "CERT")
	if err := s.Reserve(ctx, p.TokenHash, "second"); !errors.Is(err, ErrAlreadyClaimed) {
		t.Fatalf("got %v, want ErrAlreadyClaimed", err)
	}
}

func TestReservingSomethingUnknownIsNotFound(t *testing.T) {
	ctx, s := context.Background(), NewFake()
	if err := s.Reserve(ctx, HashSecret("nope"), "someone"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("got %v, want ErrNotFound", err)
	}
}

func TestDeleteRemovesTheRow(t *testing.T) {
	// Called once the device has collected. After that the certificate exists
	// only on the device and in AWS IoT; nothing here can hand it out again.
	ctx, s := context.Background(), NewFake()
	p, _, token := pending(t)
	if err := s.Create(ctx, p); err != nil {
		t.Fatal(err)
	}
	if err := s.Delete(ctx, p.TokenHash); err != nil {
		t.Fatal(err)
	}
	if _, found, _ := s.ByTokenHash(ctx, HashSecret(token)); found {
		t.Error("the row survived Delete")
	}
}

func TestDeletingSomethingAlreadyGoneIsFine(t *testing.T) {
	// The device may retry a collection whose delete already succeeded.
	ctx, s := context.Background(), NewFake()
	if err := s.Delete(ctx, HashSecret("nope")); err != nil {
		t.Errorf("Delete of an absent row returned %v", err)
	}
}

func TestTwoEnrollmentsCannotShareACode(t *testing.T) {
	// Uniqueness enforced at write. Detecting a collision afterwards would
	// mean two panels whose codes are interchangeable, and whoever typed one
	// would get whichever the lookup happened to return.
	ctx, s := context.Background(), NewFake()
	first, _, _ := pending(t)
	if err := s.Create(ctx, first); err != nil {
		t.Fatal(err)
	}
	second, _, _ := pending(t)
	second.CodeHash = first.CodeHash
	if err := s.Create(ctx, second); !errors.Is(err, ErrCodeTaken) {
		t.Fatalf("a duplicate code was accepted: %v", err)
	}
}

func TestRotateCodeReplacesTheOldOne(t *testing.T) {
	// The code is on a screen anybody in the room can read, so it rotates
	// while the panel waits. The old one must stop working.
	ctx, s := context.Background(), NewFake()
	p, oldCode, _ := pending(t)
	if err := s.Create(ctx, p); err != nil {
		t.Fatal(err)
	}
	newCode, err := Code()
	if err != nil {
		t.Fatal(err)
	}
	fresh := time.Now().Add(15 * time.Minute).Unix()
	if err := s.RotateCode(ctx, p.TokenHash, HashSecret(newCode), fresh); err != nil {
		t.Fatal(err)
	}
	if _, found, _ := s.ByCodeHash(ctx, HashSecret(oldCode)); found {
		t.Error("the old code still resolves")
	}
	got, found, _ := s.ByCodeHash(ctx, HashSecret(newCode))
	if !found || got.TokenHash != p.TokenHash {
		t.Errorf("the new code resolved to %+v", got)
	}
}

func TestASupersededCodeStaysDeadAcrossMultipleRotations(t *testing.T) {
	// Guards against the Dynamo-specific version of this: two RotateCode
	// transactions racing each other can leave a stale reservation pointing
	// at an enrollment that has since rotated again, and that enrollment's
	// CodeExpiresAt is refreshed each time so the stale row looks fresh.
	// Dynamo.ByCodeHash closes this by checking the loaded row's own CodeHash
	// against the hash that was looked up. The Fake's single-row-per-token
	// model cannot reproduce the race itself (RotateCode holds the lock for
	// its whole duration here), but it must still uphold the same contract:
	// every code that ever pointed at this enrollment except the current one
	// stays dead, not just the immediately-previous one.
	ctx, s := context.Background(), NewFake()
	p, firstCode, _ := pending(t)
	if err := s.Create(ctx, p); err != nil {
		t.Fatal(err)
	}
	secondCode, err := Code()
	if err != nil {
		t.Fatal(err)
	}
	if err := s.RotateCode(ctx, p.TokenHash, HashSecret(secondCode), time.Now().Add(15*time.Minute).Unix()); err != nil {
		t.Fatal(err)
	}
	thirdCode, err := Code()
	if err != nil {
		t.Fatal(err)
	}
	if err := s.RotateCode(ctx, p.TokenHash, HashSecret(thirdCode), time.Now().Add(15*time.Minute).Unix()); err != nil {
		t.Fatal(err)
	}
	for _, old := range []string{firstCode, secondCode} {
		if _, found, _ := s.ByCodeHash(ctx, HashSecret(old)); found {
			t.Errorf("a superseded code (%q) still resolves", old)
		}
	}
	got, found, _ := s.ByCodeHash(ctx, HashSecret(thirdCode))
	if !found || got.TokenHash != p.TokenHash {
		t.Errorf("the current code resolved to %+v", got)
	}
}

func TestRotateCodeRefusesACodeInUse(t *testing.T) {
	ctx, s := context.Background(), NewFake()
	first, _, _ := pending(t)
	second, _, _ := pending(t)
	if err := s.Create(ctx, first); err != nil {
		t.Fatal(err)
	}
	if err := s.Create(ctx, second); err != nil {
		t.Fatal(err)
	}
	err := s.RotateCode(ctx, second.TokenHash, first.CodeHash, time.Now().Add(time.Minute).Unix())
	if !errors.Is(err, ErrCodeTaken) {
		t.Fatalf("got %v, want ErrCodeTaken", err)
	}
}

func TestRotateCodeRefusesOnceClaimed(t *testing.T) {
	// A claimed enrollment must not have its code swapped out from under the
	// certificate being minted for it.
	ctx, s := context.Background(), NewFake()
	p, _, _ := pending(t)
	_ = s.Create(ctx, p)
	_ = s.Reserve(ctx, p.TokenHash, "owner-1")
	newCode, _ := Code()
	if err := s.RotateCode(ctx, p.TokenHash, HashSecret(newCode), time.Now().Unix()); !errors.Is(err, ErrAlreadyClaimed) {
		t.Fatalf("got %v, want ErrAlreadyClaimed", err)
	}
}

func TestTheOwnerHintRoundTrips(t *testing.T) {
	ctx, s := context.Background(), NewFake()
	p, _, token := pending(t)
	p.OwnerHintHash = HashSecret(NormalizeOwner("Friend@Example.com"))
	if err := s.Create(ctx, p); err != nil {
		t.Fatal(err)
	}
	got, _, _ := s.ByTokenHash(ctx, HashSecret(token))
	if got.OwnerHintHash != HashSecret("friend@example.com") {
		t.Errorf("owner hint came back as %q", got.OwnerHintHash)
	}
}

func TestCreateRequiresAnExpiry(t *testing.T) {
	// A pending row with no TTL never expires, and junk rows are the entire
	// abuse surface of the unauthenticated endpoint.
	ctx, s := context.Background(), NewFake()
	p, _, _ := pending(t)
	p.ExpiresAt = 0
	if err := s.Create(ctx, p); err == nil {
		t.Fatal("a row with no expiry was accepted")
	}
}
