# Enrollment Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a freshly flashed panel obtain its own AWS IoT certificate — generating its key locally, showing a code, and becoming a real device the moment an invited owner types that code — with nobody provisioning it by hand.

**Architecture:** A new `internal/enroll` package holds the domain (codes, tokens, hashing, CSR validation) and two narrow ports: a `Store` over a new `scoreboard-enrollments` DynamoDB table, and an `Issuer` over AWS IoT. A second Lambda, `scoreboard-enroll`, owns every enrollment route and is the only identity in the account permitted to mint device certificates. The existing admin API keeps its current permissions and loses its claim route.

**Tech Stack:** Go 1.27, `aws-lambda-go`, `aws-sdk-go-v2` (dynamodb, iot), `crypto/x509`, API Gateway HTTP API, Terraform.

**Spec:** `docs/superpowers/specs/2026-09-12-device-enrollment-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **US spelling everywhere** — `enroll`, `enrollment`, never `enrol`/`enrolment`. This binds prose, identifiers, file names, table names and endpoint paths alike.
- **Nothing is trusted from the CSR.** Not the subject, not the SANs, not any extension. The thing name is assigned server-side. A CSR is a public key plus a proof of possession; it is not a request for a name.
- **No certificate, thing, or policy attachment is created before an authenticated owner claims a code.** This is the invariant the whole design rests on; every other property here is decoration if it fails.
- **`iot:AttachPolicy` is scoped to `cert/*`, not to a policy ARN.** AWS authorizes that action against its target certificate, not against the policy, so a policy-scoped statement matches nothing and fails every claim -- an earlier version of this plan got that wrong. Which policy is attached is constrained instead by the issuer fixing the name at construction, by the role holding no policy-authoring actions, and by there being one IoT policy in the account. The last of those is a contingency worth watching, not a guarantee.
- **Both secrets are stored hashed.** An enrollment is keyed by `sha256(collection token)`; a code is found through a reservation item keyed by `sha256(code)`, which is also what makes codes unique. Raw values must never reach a stored item, a log line, or an error message.
- **A pre-bound enrollment is claimable only by its owner.** When a card carried an `owner=` line, the claim must refuse any caller whose `email`/`cognito:username` claim does not match it. This is what stops somebody who read the code off the screen.
- **404 never 403.** A wrong collection token and a nonexistent one must be indistinguishable, exactly as the admin API already treats a device the caller does not own.
- **Tests run with no AWS and no network.** Every port has a `Fake`; no test may construct a real AWS client.
- **Never commit anything under `device/config/`, or any key, certificate or credential.** Both repositories are public.
- **Do not run `terraform apply`, `make deploy`, `make provision`, `tools/provision.sh`, any AWS mutating call, or `git push`.** This plan writes Terraform; applying it is a separate, human-approved step.

## File Structure

**New:**

| File | Responsibility |
|---|---|
| `cloud/internal/enroll/enroll.go` | Codes, collection tokens, secret hashing, thing-name generation, input normalization. Pure; no AWS, no I/O |
| `cloud/internal/enroll/csr.go` | CSR parsing and validation — the one input an anonymous stranger fully controls |
| `cloud/internal/enroll/store.go` | The `Pending` record, the `Store` interface, and an in-memory `Fake` |
| `cloud/internal/enroll/dynamo.go` | `Store` over DynamoDB, including the TTL attribute and the one-time claim condition |
| `cloud/internal/enroll/issuer.go` | The `Issuer` port over AWS IoT, with cleanup on partial failure, and a `Fake` |
| `cloud/cmd/enroll/handler.go` | The three enrollment routes |
| `cloud/cmd/enroll/main.go` | Lambda wiring |
| `terraform/enroll.tf` | The table, the Lambda, its role, the routes, per-route throttling, and the alarms |

**Modified:** `cloud/cmd/api/handler.go` (remove the claim route), `cloud/cmd/api/handler_test.go`, `terraform/admin.tf` (move the claim route's integration), `Makefile` (build the new binary).

**Why enrollment is a second Lambda:** certificate minting needs four IoT actions the admin API role does not have and should not gain. Every route in the admin API is reachable by any signed-in user; putting fleet-identity creation behind that same credential widens the blast radius of every bug in it.

**Why the claim route moves:** claiming is the moment a certificate is minted, so it has to run where the minting permissions live. The admin API keeps list, rename, retarget and unbind.

**Residual risk, recorded rather than hidden:** the unauthenticated submit route and the minting code then share one Lambda and one role. They are separate dispatch paths in a memory-safe language, and the unauthenticated path does nothing but a size-capped PEM decode, so the realistic coupling is the shared role rather than code execution. Splitting into a third function would halve that and double the deploy surface; if a review disagrees, the split is cheap to make later because `Issuer` is already a port.

---

### Task 1: Codes, tokens and names

**Files:**
- Create: `cloud/internal/enroll/enroll.go`
- Test: `cloud/internal/enroll/enroll_test.go`

**Interfaces:**
- Consumes: nothing.
- Produces: `Alphabet` (const string); `Code() (string, error)`; `Token() (string, error)`; `ThingName() (string, error)`; `HashSecret(s string) string`; `NormalizeCode(input string) string`; `FormatCode(code string) string`; `NormalizeOwner(input string) string`.

Pure functions, no AWS, no I/O. The pairing code is typed by a human off a screen across a room, so the alphabet and the normalization matter more than they look.

- [ ] **Step 1: Write the failing tests**

Create `cloud/internal/enroll/enroll_test.go`:

```go
package enroll

import (
	"strings"
	"testing"
)

func TestAlphabetExcludesAmbiguousCharacters(t *testing.T) {
	// Typed off a screen from across a room. 0/O and 1/I/L are the pairs
	// people get wrong, and a wrong character is indistinguishable from an
	// expired code to the person typing it.
	for _, bad := range []string{"0", "O", "1", "I", "L", "U"} {
		if strings.Contains(Alphabet, bad) {
			t.Errorf("alphabet contains ambiguous %q", bad)
		}
	}
	if len(Alphabet) != 30 {
		t.Errorf("alphabet is %d characters, want 30", len(Alphabet))
	}
}

func TestCodeShape(t *testing.T) {
	code, err := Code()
	if err != nil {
		t.Fatal(err)
	}
	if len(code) != 8 {
		t.Fatalf("code %q is %d characters, want 8", code, len(code))
	}
	for _, r := range code {
		if !strings.ContainsRune(Alphabet, r) {
			t.Errorf("code %q contains %q, which is not in the alphabet", code, r)
		}
	}
}

func TestCodesDiffer(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		code, err := Code()
		if err != nil {
			t.Fatal(err)
		}
		if seen[code] {
			t.Fatalf("Code() repeated %q within 200 draws", code)
		}
		seen[code] = true
	}
}

func TestTokenIsLongAndUnpredictable(t *testing.T) {
	a, err := Token()
	if err != nil {
		t.Fatal(err)
	}
	b, _ := Token()
	if a == b {
		t.Fatal("two tokens were identical")
	}
	// 32 bytes base64url, unpadded.
	if len(a) < 43 {
		t.Fatalf("token %q is only %d characters", a, len(a))
	}
}

func TestThingNameIsPrefixedAndSafe(t *testing.T) {
	name, err := ThingName()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(name, "scoreboard-") {
		t.Fatalf("thing name %q lacks the scoreboard- prefix", name)
	}
	// AWS IoT accepts [a-zA-Z0-9:_-]; the devices package is stricter still.
	for _, r := range name {
		ok := r == '-' || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')
		if !ok {
			t.Errorf("thing name %q contains %q", name, r)
		}
	}
}

func TestHashSecretIsStableAndHidesTheInput(t *testing.T) {
	h := HashSecret("ABCD2345")
	if h == "ABCD2345" {
		t.Fatal("HashSecret returned its input")
	}
	if h != HashSecret("ABCD2345") {
		t.Fatal("HashSecret is not stable")
	}
	if len(h) != 64 {
		t.Fatalf("hash %q is %d characters, want 64 hex", h, len(h))
	}
}

func TestNormalizeCodeAcceptsWhatPeopleActuallyType(t *testing.T) {
	for _, in := range []string{"abcd2345", "ABCD-2345", "abcd 2345", " ABCD-2345 ", "AbCd-2345"} {
		if got := NormalizeCode(in); got != "ABCD2345" {
			t.Errorf("NormalizeCode(%q) = %q, want ABCD2345", in, got)
		}
	}
}

func TestFormatCodeIsReadableOnAPanel(t *testing.T) {
	if got := FormatCode("ABCD2345"); got != "ABCD-2345" {
		t.Errorf("FormatCode = %q, want ABCD-2345", got)
	}
}

func TestNormalizeOwnerMatchesHandTypedAddresses(t *testing.T) {
	// The owner hint is typed into a file on a FAT partition by a person, and
	// compared against a claim in a JWT. Both sides go through this, so a
	// trailing space or a capital letter cannot lock somebody out of their own
	// panel.
	for _, in := range []string{"Friend@Example.com", " friend@example.com ", "FRIEND@EXAMPLE.COM", "friend@example.com\n"} {
		if got := NormalizeOwner(in); got != "friend@example.com" {
			t.Errorf("NormalizeOwner(%q) = %q", in, got)
		}
	}
}

func TestNormalizeOwnerDoesNotStripPlusAddressing(t *testing.T) {
	// friend+panel@example.com is a different address from friend@example.com
	// as far as Cognito is concerned, so it must stay different here.
	if got := NormalizeOwner("Friend+Panel@example.com"); got != "friend+panel@example.com" {
		t.Errorf("got %q", got)
	}
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd cloud && go test ./internal/enroll/ -run . -v 2>&1 | head -20`
Expected: FAIL — the package does not exist.

- [ ] **Step 3: Implement**

Create `cloud/internal/enroll/enroll.go`:

```go
// Package enroll turns a freshly flashed panel into a registered device: it
// mints the short code a person reads off the screen, the long token the
// device proves itself with, and the name the thing will carry.
package enroll

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"math/big"
	"strings"
)

// Alphabet is Crockford base32 without the characters people mistype off a
// screen: 0 and O, 1 and I and L. U is out too, as Crockford has it, so a
// random draw cannot spell something unfortunate.
const Alphabet = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

const (
	codeLength      = 8
	thingSuffixSize = 12
	tokenBytes      = 32
)

func randomString(n int) (string, error) {
	limit := big.NewInt(int64(len(Alphabet)))
	out := make([]byte, n)
	for i := range out {
		// crypto/rand.Int is uniform over [0, limit) -- no modulo bias, which
		// a plain "read a byte and take it mod 30" would introduce.
		pick, err := rand.Int(rand.Reader, limit)
		if err != nil {
			return "", err
		}
		out[i] = Alphabet[pick.Int64()]
	}
	return string(out), nil
}

// Code is the pairing code shown on the panel. Short enough to read across a
// room, and only useful while the enrollment it belongs to is unclaimed.
func Code() (string, error) { return randomString(codeLength) }

// Token is the device's proof that it is the panel that asked to enroll. It
// is never displayed and never typed: the pairing code is on a screen anyone
// in the room can see, so the code alone must not be enough to collect a
// certificate.
func Token() (string, error) {
	raw := make([]byte, tokenBytes)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

// ThingName is assigned here rather than taken from the CSR, so that nothing
// a caller sends can name or collide with an existing device.
func ThingName() (string, error) {
	suffix, err := randomString(thingSuffixSize)
	if err != nil {
		return "", err
	}
	return "scoreboard-" + strings.ToLower(suffix), nil
}

// HashSecret is what gets stored. A dump of the enrollments table then yields
// nobody a usable collection token and nobody a claimable code -- the same
// reasoning as not storing passwords, applied to two short-lived secrets that
// would otherwise sit in plaintext beside the certificates they unlock.
func HashSecret(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// NormalizeCode accepts what a person actually types: any case, with or
// without the separator, with stray whitespace.
func NormalizeCode(input string) string {
	var b strings.Builder
	for _, r := range strings.ToUpper(input) {
		if strings.ContainsRune(Alphabet, r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// NormalizeOwner puts a hand-typed owner hint and a JWT claim into the same
// shape so they can be compared. Case and surrounding whitespace only: plus
// addressing and dots are meaningful to Cognito, so they are meaningful here.
func NormalizeOwner(input string) string {
	return strings.ToLower(strings.TrimSpace(input))
}

// FormatCode splits the code for display. Four and four is easier to hold in
// your head while you walk to a laptop.
func FormatCode(code string) string {
	if len(code) != codeLength {
		return code
	}
	return code[:4] + "-" + code[4:]
}
```

- [ ] **Step 4: Run the tests**

Run: `cd cloud && go test ./internal/enroll/ -v 2>&1 | tail -20`
Expected: PASS, all cases.

- [ ] **Step 5: Commit**

```bash
git add cloud/internal/enroll/enroll.go cloud/internal/enroll/enroll_test.go
git commit -m "enroll: codes a person can read off a screen, tokens they cannot"
```

---

### Task 2: CSR validation

**Files:**
- Create: `cloud/internal/enroll/csr.go`
- Test: `cloud/internal/enroll/csr_test.go`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `MaxCSRBytes` (const); `ParseCSR(pemBytes []byte) error`. It returns no parsed request on purpose: the subject, SANs and extensions must not travel out to a caller who might reach into them, so "nothing is trusted from the CSR" is enforced by the compiler rather than by discipline.

This is the one input an anonymous stranger fully controls, which is why it gets its own task and its own review gate. Four things must hold: the body is size-capped, the key is P-256 and nothing else, the signature verifies (that is the proof the sender holds the private key), and **nothing in the subject is read or returned**.

- [ ] **Step 1: Write the failing tests**

Create `cloud/internal/enroll/csr_test.go`:

```go
package enroll

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"strings"
	"testing"
)

func makeCSR(t *testing.T, key any, subject string) []byte {
	t.Helper()
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: subject},
	}, key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der})
}

func p256(t *testing.T) *ecdsa.PrivateKey {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func TestParseCSRAcceptsAP256Request(t *testing.T) {
	if _, err := ParseCSR(makeCSR(t, p256(t), "whatever")); err != nil {
		t.Fatalf("a valid P-256 CSR was rejected: %v", err)
	}
}

func TestParseCSRRejectsAnOversizedBody(t *testing.T) {
	huge := make([]byte, MaxCSRBytes+1)
	if _, err := ParseCSR(huge); err == nil {
		t.Fatal("an oversized body was accepted")
	}
}

func TestParseCSRRejectsRubbish(t *testing.T) {
	for name, body := range map[string][]byte{
		"empty":        {},
		"not pem":      []byte("hello"),
		"wrong block":  pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: []byte{1, 2, 3}}),
		"bad der":      pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: []byte{1, 2, 3}}),
	} {
		if _, err := ParseCSR(body); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestParseCSRRejectsRSA(t *testing.T) {
	// Not taste: an attacker who can pick the algorithm can pick an expensive
	// one and bill us for the CPU.
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseCSR(makeCSR(t, key, "rsa")); err == nil {
		t.Fatal("an RSA CSR was accepted")
	}
}

func TestParseCSRRejectsTheWrongCurve(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseCSR(makeCSR(t, key, "p384")); err == nil {
		t.Fatal("a P-384 CSR was accepted")
	}
}

func TestParseCSRRejectsABrokenSignature(t *testing.T) {
	// The signature is the sender's proof that it holds the private key. A CSR
	// that does not verify is somebody replaying a public key they found.
	body := makeCSR(t, p256(t), "tampered")
	block, _ := pem.Decode(body)
	block.Bytes[len(block.Bytes)-1] ^= 0xff
	if _, err := ParseCSR(pem.EncodeToMemory(block)); err == nil {
		t.Fatal("a CSR with a broken signature was accepted")
	}
}

func TestParseCSRDoesNotLeakTheSubjectIntoTheError(t *testing.T) {
	// Nothing downstream may use the subject, and an error message is the
	// easiest place for it to escape into a log.
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	_, parseErr := ParseCSR(makeCSR(t, key, "scoreboard-victim"))
	if parseErr == nil {
		t.Fatal("expected rejection")
	}
	if strings.Contains(parseErr.Error(), "scoreboard-victim") {
		t.Errorf("the CSR subject reached the error message: %v", parseErr)
	}
}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd cloud && go test ./internal/enroll/ -run CSR -v 2>&1 | head -20`
Expected: FAIL — `undefined: ParseCSR`.

- [ ] **Step 3: Implement**

Create `cloud/internal/enroll/csr.go`:

```go
package enroll

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
)

// MaxCSRBytes caps the request body. A P-256 CSR is a few hundred bytes; this
// leaves generous room while refusing anything sent to waste our time.
const MaxCSRBytes = 4096

// ParseCSR validates a certificate signing request from an unauthenticated
// caller.
//
// Note what it does NOT do: it never reads the subject, the SANs, or any
// extension, and the caller must not either. A CSR here is a public key plus a
// proof of possession. It is not a request for a name -- the thing name is
// assigned by ThingName(), server-side, so that nothing a stranger sends can
// name a device or collide with one that exists.
func ParseCSR(pemBytes []byte) (*x509.CertificateRequest, error) {
	if len(pemBytes) == 0 {
		return nil, errors.New("empty certificate request")
	}
	if len(pemBytes) > MaxCSRBytes {
		return nil, fmt.Errorf("certificate request is %d bytes; the maximum is %d", len(pemBytes), MaxCSRBytes)
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil || block.Type != "CERTIFICATE REQUEST" {
		return nil, errors.New("not a PEM certificate request")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		// Deliberately not wrapped: x509's errors can quote parts of the
		// input, and the input is attacker-supplied.
		return nil, errors.New("malformed certificate request")
	}
	// The signature is the sender's proof it holds the matching private key.
	// Without this check anyone could enroll a public key they found.
	if err := csr.CheckSignature(); err != nil {
		return nil, errors.New("certificate request signature does not verify")
	}
	pub, ok := csr.PublicKey.(*ecdsa.PublicKey)
	if !ok {
		return nil, errors.New("key must be ECDSA P-256")
	}
	if pub.Curve != elliptic.P256() {
		return nil, errors.New("key must be ECDSA P-256")
	}
	return csr, nil
}
```

- [ ] **Step 4: Run the tests**

Run: `cd cloud && go test ./internal/enroll/ -v 2>&1 | tail -20`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cloud/internal/enroll/csr.go cloud/internal/enroll/csr_test.go
git commit -m "enroll: validate the one input a stranger fully controls"
```

---

### Task 3: The pending-enrollment store

**Files:**
- Create: `cloud/internal/enroll/store.go`, `cloud/internal/enroll/dynamo.go`
- Test: `cloud/internal/enroll/store_test.go`

**Interfaces:**
- Consumes: `HashSecret` from Task 1.
- Produces: `Pending` struct; `Store` interface with `Create`, `ByCodeHash`, `ByTokenHash`, `RotateCode`, `Reserve`, `SetCertificate`, `Delete`; `ErrCodeTaken`; `StatusPending`/`StatusClaiming`/`StatusReady`; `ErrNotFound`, `ErrAlreadyClaimed`; `Fake` (with `NewFake()`); `NewDynamo(client *dynamodb.Client, table string) *Dynamo`.

**Three states, not two, and the reason is a race.** Claiming and issuing a certificate cannot be one atomic step: issuing calls AWS. If the one-time guard ran *after* issuing, two people racing the same code would both mint a certificate before either lost. So `Reserve` moves `pending -> claiming` atomically and is the one-time guard; issuance happens after it; `SetCertificate` moves `claiming -> ready`.

Mirrors `internal/devices`: an interface, an in-memory `Fake` that every other test uses, and a `Dynamo` that is the only thing knowing about AWS.

A separate table from `scoreboard-devices` on purpose. That table is the irreplaceable ownership record — SCO-23 exists because losing it means every owner loses every panel — and anonymous internet traffic must not churn writes into it.

- [ ] **Step 1: Write the failing tests**

Create `cloud/internal/enroll/store_test.go`:

```go
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd cloud && go test ./internal/enroll/ -run Fake -v 2>&1 | head -10`
Expected: FAIL — `undefined: NewFake`.

- [ ] **Step 3: Write the interface and the Fake**

Create `cloud/internal/enroll/store.go`:

```go
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
```

- [ ] **Step 4: Run the tests**

Run: `cd cloud && go test ./internal/enroll/ -v 2>&1 | tail -15`
Expected: PASS.

- [ ] **Step 5: Write the DynamoDB implementation**

Create `cloud/internal/enroll/dynamo.go`.

**Two items per enrollment, and no GSI.** DynamoDB cannot enforce uniqueness on
a secondary index, so the code gets a *reservation item* of its own —
`code#<hash>` — written in the same transaction as the enrollment and
conditioned on not already existing. That makes uniqueness a property of the
write rather than something checked afterwards, and it happens to remove the
index: looking a code up is now a plain `GetItem` on the reservation, which
points at the enrollment.

```go
package enroll

import (
	"context"
	"errors"
	"strconv"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

// Dynamo is the Store over DynamoDB. The table carries a TTL on expiresAt, so
// expiry is the database's job rather than a sweeper's -- for the enrollment
// and for its code reservation alike.
type Dynamo struct {
	client *dynamodb.Client
	table  string
}

func NewDynamo(client *dynamodb.Client, table string) *Dynamo {
	return &Dynamo{client: client, table: table}
}

func s(v string) types.AttributeValue  { return &types.AttributeValueMemberS{Value: v} }
func n(v int64) types.AttributeValue   { return &types.AttributeValueMemberN{Value: strconv.FormatInt(v, 10)} }
func codeKey(codeHash string) string   { return "code#" + codeHash }

func (x *Dynamo) enrollmentItem(p Pending) map[string]types.AttributeValue {
	item := map[string]types.AttributeValue{
		"pk":            s(p.TokenHash),
		"codeHash":      s(p.CodeHash),
		"codeExpiresAt": n(p.CodeExpiresAt),
		"csr":           s(p.CSR),
		"thingName":     s(p.ThingName),
		"status":        s(StatusPending),
		"expiresAt":     n(p.ExpiresAt),
	}
	if p.OwnerHintHash != "" {
		item["ownerHintHash"] = s(p.OwnerHintHash)
	}
	return item
}

// reservation is the item that makes a code unique. It expires with the code,
// so a rotated or abandoned code frees itself.
func (x *Dynamo) reservation(codeHash, tokenHash string, expiresAt int64) map[string]types.AttributeValue {
	return map[string]types.AttributeValue{
		"pk":        s(codeKey(codeHash)),
		"tokenHash": s(tokenHash),
		"expiresAt": n(expiresAt),
	}
}

func conditionFailed(err error) bool {
	var single *types.ConditionalCheckFailedException
	if errors.As(err, &single) {
		return true
	}
	var canceled *types.TransactionCanceledException
	if errors.As(err, &canceled) {
		for _, reason := range canceled.CancellationReasons {
			if aws.ToString(reason.Code) == "ConditionalCheckFailed" {
				return true
			}
		}
	}
	return false
}

func (x *Dynamo) Create(ctx context.Context, p Pending) error {
	if p.ExpiresAt == 0 {
		return errors.New("enroll: an enrollment needs an expiry")
	}
	_, err := x.client.TransactWriteItems(ctx, &dynamodb.TransactWriteItemsInput{
		TransactItems: []types.TransactWriteItem{
			{Put: &types.Put{
				TableName:           aws.String(x.table),
				Item:                x.enrollmentItem(p),
				ConditionExpression: aws.String("attribute_not_exists(pk)"),
			}},
			{Put: &types.Put{
				TableName:           aws.String(x.table),
				Item:                x.reservation(p.CodeHash, p.TokenHash, p.CodeExpiresAt),
				ConditionExpression: aws.String("attribute_not_exists(pk)"),
			}},
		},
	})
	if err != nil && conditionFailed(err) {
		return ErrCodeTaken
	}
	return err
}

// RotateCode swaps one code for another atomically: the old reservation goes,
// the new one is claimed, and the enrollment points at it. A code already in
// use fails the condition, and the caller generates another.
func (x *Dynamo) RotateCode(ctx context.Context, tokenHash, newCodeHash string, expiresAt int64) error {
	current, found, err := x.ByTokenHash(ctx, tokenHash)
	if err != nil {
		return err
	}
	if !found {
		return ErrNotFound
	}
	_, err = x.client.TransactWriteItems(ctx, &dynamodb.TransactWriteItemsInput{
		TransactItems: []types.TransactWriteItem{
			{Delete: &types.Delete{
				TableName: aws.String(x.table),
				Key:       map[string]types.AttributeValue{"pk": s(codeKey(current.CodeHash))},
			}},
			{Put: &types.Put{
				TableName:           aws.String(x.table),
				Item:                x.reservation(newCodeHash, tokenHash, expiresAt),
				ConditionExpression: aws.String("attribute_not_exists(pk)"),
			}},
			{Update: &types.Update{
				TableName:                 aws.String(x.table),
				Key:                       map[string]types.AttributeValue{"pk": s(tokenHash)},
				UpdateExpression:          aws.String("SET codeHash = :c, codeExpiresAt = :e"),
				ConditionExpression:       aws.String("#s = :pending"),
				ExpressionAttributeNames:  map[string]string{"#s": "status"},
				ExpressionAttributeValues: map[string]types.AttributeValue{
					":c":       s(newCodeHash),
					":e":       n(expiresAt),
					":pending": s(StatusPending),
				},
			}},
		},
	})
	if err != nil && conditionFailed(err) {
		return ErrCodeTaken
	}
	return err
}

func (x *Dynamo) ByTokenHash(ctx context.Context, tokenHash string) (Pending, bool, error) {
	out, err := x.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName: aws.String(x.table),
		Key:       map[string]types.AttributeValue{"pk": s(tokenHash)},
	})
	if err != nil {
		return Pending{}, false, err
	}
	if out.Item == nil {
		return Pending{}, false, nil
	}
	p, err := unmarshalPending(out.Item)
	return p, err == nil, err
}

// ByCodeHash follows the reservation to the enrollment. No index: the
// reservation item exists to make codes unique, and pointing at its owner is
// free.
func (x *Dynamo) ByCodeHash(ctx context.Context, codeHash string) (Pending, bool, error) {
	out, err := x.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName: aws.String(x.table),
		Key:       map[string]types.AttributeValue{"pk": s(codeKey(codeHash))},
	})
	if err != nil {
		return Pending{}, false, err
	}
	if out.Item == nil {
		return Pending{}, false, nil
	}
	pointer, ok := out.Item["tokenHash"].(*types.AttributeValueMemberS)
	if !ok {
		return Pending{}, false, errors.New("enroll: malformed code reservation")
	}
	return x.ByTokenHash(ctx, pointer.Value)
}

// Reserve is the one-time guard. The condition is what enforces it: an
// enrollment that has left StatusPending fails rather than having its owner
// overwritten. It deliberately runs before any AWS IoT call, so two callers
// racing the same code cannot both mint a certificate.
func (x *Dynamo) Reserve(ctx context.Context, tokenHash, owner string) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:           aws.String(x.table),
		Key:                 map[string]types.AttributeValue{"pk": s(tokenHash)},
		UpdateExpression:    aws.String("SET #s = :claiming, #o = :owner"),
		ConditionExpression: aws.String("attribute_exists(pk) AND #s = :pending"),
		ExpressionAttributeNames: map[string]string{
			"#s": "status", // reserved word
			"#o": "owner",  // reserved word
		},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":claiming": s(StatusClaiming),
			":pending":  s(StatusPending),
			":owner":    s(owner),
		},
		// Lets us tell "no such enrollment" from "already claimed" without a
		// second read, the same way devices.Claim does.
		ReturnValuesOnConditionCheckFailure: types.ReturnValuesOnConditionCheckFailureAllOld,
	})
	if err == nil {
		return nil
	}
	var failed *types.ConditionalCheckFailedException
	if errors.As(err, &failed) {
		if failed.Item == nil {
			return ErrNotFound
		}
		return ErrAlreadyClaimed
	}
	return err
}

// SetCertificate completes what Reserve began.
func (x *Dynamo) SetCertificate(ctx context.Context, tokenHash, certPEM string) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                aws.String(x.table),
		Key:                      map[string]types.AttributeValue{"pk": s(tokenHash)},
		UpdateExpression:         aws.String("SET #s = :ready, certPem = :cert"),
		ConditionExpression:      aws.String("attribute_exists(pk)"),
		ExpressionAttributeNames: map[string]string{"#s": "status"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":ready": s(StatusReady),
			":cert":  s(certPEM),
		},
	})
	return err
}

// Delete removes the enrollment and frees its code.
func (x *Dynamo) Delete(ctx context.Context, tokenHash string) error {
	current, found, err := x.ByTokenHash(ctx, tokenHash)
	if err != nil {
		return err
	}
	if !found {
		return nil // already gone; a device may retry a collection that succeeded
	}
	_, err = x.client.TransactWriteItems(ctx, &dynamodb.TransactWriteItemsInput{
		TransactItems: []types.TransactWriteItem{
			{Delete: &types.Delete{
				TableName: aws.String(x.table),
				Key:       map[string]types.AttributeValue{"pk": s(tokenHash)},
			}},
			{Delete: &types.Delete{
				TableName: aws.String(x.table),
				Key:       map[string]types.AttributeValue{"pk": s(codeKey(current.CodeHash))},
			}},
		},
	})
	return err
}

func unmarshalPending(item map[string]types.AttributeValue) (Pending, error) {
	get := func(key string) string {
		if v, ok := item[key].(*types.AttributeValueMemberS); ok {
			return v.Value
		}
		return ""
	}
	num := func(key string) int64 {
		if v, ok := item[key].(*types.AttributeValueMemberN); ok {
			parsed, err := strconv.ParseInt(v.Value, 10, 64)
			if err == nil {
				return parsed
			}
		}
		return 0
	}
	p := Pending{
		TokenHash:     get("pk"),
		CodeHash:      get("codeHash"),
		CodeExpiresAt: num("codeExpiresAt"),
		OwnerHintHash: get("ownerHintHash"),
		CSR:           get("csr"),
		ThingName:     get("thingName"),
		Status:        get("status"),
		CertPEM:       get("certPem"),
		Owner:         get("owner"),
		ExpiresAt:     num("expiresAt"),
	}
	if p.TokenHash == "" || p.ThingName == "" {
		return Pending{}, errors.New("enroll: malformed enrollment row")
	}
	return p, nil
}
```

- [ ] **Step 6: Verify it compiles and the suite is green**

Run: `cd cloud && go vet ./... && go test ./... 2>&1 | tail -10`
Expected: PASS, no vet complaints.

- [ ] **Step 7: Commit**

```bash
git add cloud/internal/enroll/store.go cloud/internal/enroll/dynamo.go cloud/internal/enroll/store_test.go
git commit -m "enroll: pending enrollments, hashed secrets, and a one-time reserve"
```

---

### Task 4: The certificate issuer

**Files:**
- Create: `cloud/internal/enroll/issuer.go`
- Test: `cloud/internal/enroll/issuer_test.go`
- Modify: `cloud/go.mod`, `cloud/go.sum` (add `aws-sdk-go-v2/service/iot` — the repo currently has only `iotdataplane`, which is the data plane and cannot register anything)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Issuer` interface with `Issue(ctx context.Context, csrPEM, thingName string) (string, error)`; `IoTAPI` interface; `NewIoTIssuer(api IoTAPI, policyName string) *IoTIssuer`; `FakeIssuer` with fields `CertPEM string`, `Err error`, `Calls []string`.

Four AWS calls have to succeed together: sign the CSR, create the thing, attach the principal, attach the policy. **If any fails, the ones before it must be undone.** A half-finished enrollment leaves an orphan — a certificate attached to nothing, or a thing with no policy — and orphans in an identity system are how a fleet accumulates credentials nobody can account for.

The policy name is a constructor argument rather than a parameter, so no caller can choose which policy gets attached.

- [ ] **Step 1: Write the failing tests**

Create `cloud/internal/enroll/issuer_test.go`:

```go
package enroll

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iot"
)

// stubIoT records every call and can be told to fail at one of them.
type stubIoT struct {
	calls  []string
	failAt string
}

func (s *stubIoT) record(name string) error {
	s.calls = append(s.calls, name)
	if s.failAt == name {
		return errors.New("aws said no")
	}
	return nil
}

func (s *stubIoT) CreateCertificateFromCsr(_ context.Context, in *iot.CreateCertificateFromCsrInput, _ ...func(*iot.Options)) (*iot.CreateCertificateFromCsrOutput, error) {
	if err := s.record("CreateCertificateFromCsr"); err != nil {
		return nil, err
	}
	return &iot.CreateCertificateFromCsrOutput{
		CertificateArn: aws.String("arn:aws:iot:us-east-1:1:cert/abc"),
		CertificateId:  aws.String("abc"),
		CertificatePem: aws.String("CERTPEM"),
	}, nil
}

func (s *stubIoT) CreateThing(_ context.Context, in *iot.CreateThingInput, _ ...func(*iot.Options)) (*iot.CreateThingOutput, error) {
	return &iot.CreateThingOutput{}, s.record("CreateThing")
}

func (s *stubIoT) AttachThingPrincipal(_ context.Context, in *iot.AttachThingPrincipalInput, _ ...func(*iot.Options)) (*iot.AttachThingPrincipalOutput, error) {
	return &iot.AttachThingPrincipalOutput{}, s.record("AttachThingPrincipal")
}

func (s *stubIoT) AttachPolicy(_ context.Context, in *iot.AttachPolicyInput, _ ...func(*iot.Options)) (*iot.AttachPolicyOutput, error) {
	if in.PolicyName == nil || *in.PolicyName != "scoreboard-device" {
		return nil, errors.New("wrong policy")
	}
	return &iot.AttachPolicyOutput{}, s.record("AttachPolicy")
}

func (s *stubIoT) DetachPolicy(_ context.Context, _ *iot.DetachPolicyInput, _ ...func(*iot.Options)) (*iot.DetachPolicyOutput, error) {
	return &iot.DetachPolicyOutput{}, s.record("DetachPolicy")
}

func (s *stubIoT) DetachThingPrincipal(_ context.Context, _ *iot.DetachThingPrincipalInput, _ ...func(*iot.Options)) (*iot.DetachThingPrincipalOutput, error) {
	return &iot.DetachThingPrincipalOutput{}, s.record("DetachThingPrincipal")
}

func (s *stubIoT) DeleteThing(_ context.Context, _ *iot.DeleteThingInput, _ ...func(*iot.Options)) (*iot.DeleteThingOutput, error) {
	return &iot.DeleteThingOutput{}, s.record("DeleteThing")
}

func (s *stubIoT) UpdateCertificate(_ context.Context, _ *iot.UpdateCertificateInput, _ ...func(*iot.Options)) (*iot.UpdateCertificateOutput, error) {
	return &iot.UpdateCertificateOutput{}, s.record("UpdateCertificate")
}

func (s *stubIoT) DeleteCertificate(_ context.Context, _ *iot.DeleteCertificateInput, _ ...func(*iot.Options)) (*iot.DeleteCertificateOutput, error) {
	return &iot.DeleteCertificateOutput{}, s.record("DeleteCertificate")
}

func TestIssueMakesTheFourCallsInOrder(t *testing.T) {
	stub := &stubIoT{}
	pem, err := NewIoTIssuer(stub, "scoreboard-device").Issue(context.Background(), "CSR", "scoreboard-abc")
	if err != nil {
		t.Fatal(err)
	}
	if pem != "CERTPEM" {
		t.Errorf("got certificate %q", pem)
	}
	want := "CreateCertificateFromCsr,CreateThing,AttachThingPrincipal,AttachPolicy"
	if got := strings.Join(stub.calls, ","); got != want {
		t.Errorf("calls were %q, want %q", got, want)
	}
}

func TestIssueCleansUpWhenAStepFails(t *testing.T) {
	// An orphaned certificate or a thing with no policy is a credential
	// nobody can account for. Every partial failure must unwind.
	for _, tc := range []struct {
		failAt  string
		cleanup []string
	}{
		{"CreateThing", []string{"UpdateCertificate", "DeleteCertificate"}},
		{"AttachThingPrincipal", []string{"DeleteThing", "UpdateCertificate", "DeleteCertificate"}},
		{"AttachPolicy", []string{"DetachThingPrincipal", "DeleteThing", "UpdateCertificate", "DeleteCertificate"}},
	} {
		t.Run(tc.failAt, func(t *testing.T) {
			stub := &stubIoT{failAt: tc.failAt}
			_, err := NewIoTIssuer(stub, "scoreboard-device").Issue(context.Background(), "CSR", "scoreboard-abc")
			if err == nil {
				t.Fatal("expected an error")
			}
			for _, want := range tc.cleanup {
				found := false
				for _, call := range stub.calls {
					if call == want {
						found = true
					}
				}
				if !found {
					t.Errorf("failure at %s did not call %s; calls were %v", tc.failAt, want, stub.calls)
				}
			}
		})
	}
}

func TestIssueFailsBeforeAnythingExistsWhenSigningFails(t *testing.T) {
	stub := &stubIoT{failAt: "CreateCertificateFromCsr"}
	if _, err := NewIoTIssuer(stub, "scoreboard-device").Issue(context.Background(), "CSR", "scoreboard-abc"); err == nil {
		t.Fatal("expected an error")
	}
	if len(stub.calls) != 1 {
		t.Errorf("nothing should be cleaned up when nothing was created; calls were %v", stub.calls)
	}
}

func TestFakeIssuerRecordsWhatItWasAsked(t *testing.T) {
	f := &FakeIssuer{CertPEM: "PEM"}
	got, err := f.Issue(context.Background(), "CSR", "scoreboard-xyz")
	if err != nil || got != "PEM" {
		t.Fatalf("got %q, %v", got, err)
	}
	if len(f.Calls) != 1 || f.Calls[0] != "scoreboard-xyz" {
		t.Errorf("calls were %v", f.Calls)
	}
}
```

- [ ] **Step 2: Add the SDK dependency**

Run: `cd cloud && go get github.com/aws/aws-sdk-go-v2/service/iot@latest && go mod tidy`
Expected: `go.mod` gains `aws-sdk-go-v2/service/iot` as a direct dependency.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd cloud && go test ./internal/enroll/ -run Issue -v 2>&1 | head -10`
Expected: FAIL — `undefined: NewIoTIssuer`.

- [ ] **Step 4: Implement**

Create `cloud/internal/enroll/issuer.go`:

```go
package enroll

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iot"
	"github.com/aws/aws-sdk-go-v2/service/iot/types"
)

// Issuer turns a validated certificate signing request into a working device
// identity. It is a port so that nothing but IoTIssuer needs AWS, and so the
// handler's tests can run without it.
type Issuer interface {
	Issue(ctx context.Context, csrPEM, thingName string) (certPEM string, err error)
}

// IoTAPI is the slice of the AWS IoT control plane this needs. Narrow on
// purpose: the interface is the list of things this component is allowed to do.
type IoTAPI interface {
	CreateCertificateFromCsr(context.Context, *iot.CreateCertificateFromCsrInput, ...func(*iot.Options)) (*iot.CreateCertificateFromCsrOutput, error)
	CreateThing(context.Context, *iot.CreateThingInput, ...func(*iot.Options)) (*iot.CreateThingOutput, error)
	AttachThingPrincipal(context.Context, *iot.AttachThingPrincipalInput, ...func(*iot.Options)) (*iot.AttachThingPrincipalOutput, error)
	AttachPolicy(context.Context, *iot.AttachPolicyInput, ...func(*iot.Options)) (*iot.AttachPolicyOutput, error)
	DetachPolicy(context.Context, *iot.DetachPolicyInput, ...func(*iot.Options)) (*iot.DetachPolicyOutput, error)
	DetachThingPrincipal(context.Context, *iot.DetachThingPrincipalInput, ...func(*iot.Options)) (*iot.DetachThingPrincipalOutput, error)
	DeleteThing(context.Context, *iot.DeleteThingInput, ...func(*iot.Options)) (*iot.DeleteThingOutput, error)
	UpdateCertificate(context.Context, *iot.UpdateCertificateInput, ...func(*iot.Options)) (*iot.UpdateCertificateOutput, error)
	DeleteCertificate(context.Context, *iot.DeleteCertificateInput, ...func(*iot.Options)) (*iot.DeleteCertificateOutput, error)
}

// IoTIssuer mints device identities in AWS IoT.
//
// The policy name is fixed at construction rather than passed per call, so no
// caller can choose which policy a new certificate receives. The IAM role this
// runs under also pins iot:AttachPolicy to that one policy ARN; this is the
// same rule stated twice, because it is the difference between a bug in device
// onboarding and a compromise of the whole fleet.
type IoTIssuer struct {
	api        IoTAPI
	policyName string
}

func NewIoTIssuer(api IoTAPI, policyName string) *IoTIssuer {
	return &IoTIssuer{api: api, policyName: policyName}
}

func (i *IoTIssuer) Issue(ctx context.Context, csrPEM, thingName string) (string, error) {
	cert, err := i.api.CreateCertificateFromCsr(ctx, &iot.CreateCertificateFromCsrInput{
		CertificateSigningRequest: aws.String(csrPEM),
		SetAsActive:               true, // a plain bool in this SDK, not *bool
	})
	if err != nil {
		// Nothing was created, so there is nothing to unwind.
		return "", fmt.Errorf("signing the certificate request: %w", err)
	}
	certARN, certID := aws.ToString(cert.CertificateArn), aws.ToString(cert.CertificateId)

	if _, err := i.api.CreateThing(ctx, &iot.CreateThingInput{ThingName: aws.String(thingName)}); err != nil {
		i.rollback(ctx, thingName, certARN, certID, false, false)
		return "", fmt.Errorf("creating the thing: %w", err)
	}
	if _, err := i.api.AttachThingPrincipal(ctx, &iot.AttachThingPrincipalInput{
		ThingName: aws.String(thingName),
		Principal: aws.String(certARN),
	}); err != nil {
		i.rollback(ctx, thingName, certARN, certID, true, false)
		return "", fmt.Errorf("attaching the certificate to the thing: %w", err)
	}
	if _, err := i.api.AttachPolicy(ctx, &iot.AttachPolicyInput{
		PolicyName: aws.String(i.policyName),
		Target:     aws.String(certARN),
	}); err != nil {
		i.rollback(ctx, thingName, certARN, certID, true, true)
		return "", fmt.Errorf("attaching the device policy: %w", err)
	}
	return aws.ToString(cert.CertificatePem), nil
}

// rollback undoes as much as was done, in reverse. Best effort: a cleanup
// failure is logged and does not mask the original error, because the caller
// needs to know the enrollment failed more than it needs to know why the
// tidying afterwards also failed.
func (i *IoTIssuer) rollback(ctx context.Context, thingName, certARN, certID string, thingExists, principalAttached bool) {
	if principalAttached {
		if _, err := i.api.DetachThingPrincipal(ctx, &iot.DetachThingPrincipalInput{
			ThingName: aws.String(thingName),
			Principal: aws.String(certARN),
		}); err != nil {
			slog.Error("enroll rollback: detaching principal", "thing", thingName, "err", err)
		}
	}
	if thingExists {
		if _, err := i.api.DeleteThing(ctx, &iot.DeleteThingInput{ThingName: aws.String(thingName)}); err != nil {
			slog.Error("enroll rollback: deleting thing", "thing", thingName, "err", err)
		}
	}
	// A certificate has to be INACTIVE before it can be deleted.
	if _, err := i.api.UpdateCertificate(ctx, &iot.UpdateCertificateInput{
		CertificateId: aws.String(certID),
		NewStatus:     types.CertificateStatusInactive,
	}); err != nil {
		slog.Error("enroll rollback: deactivating certificate", "certificate", certID, "err", err)
		return // deleting will fail too; leave it INACTIVE at worst
	}
	if _, err := i.api.DeleteCertificate(ctx, &iot.DeleteCertificateInput{
		CertificateId: aws.String(certID),
	}); err != nil {
		slog.Error("enroll rollback: deleting certificate", "certificate", certID, "err", err)
	}
}

// FakeIssuer is the Issuer the handler's tests use.
type FakeIssuer struct {
	CertPEM string
	Err     error
	Calls   []string // thing names it was asked to issue for
}

func (f *FakeIssuer) Issue(_ context.Context, _, thingName string) (string, error) {
	f.Calls = append(f.Calls, thingName)
	if f.Err != nil {
		return "", f.Err
	}
	return f.CertPEM, nil
}
```

- [ ] **Step 5: Run the tests**

Run: `cd cloud && go test ./internal/enroll/ -v 2>&1 | tail -20`
Expected: PASS, including all three rollback subtests.

- [ ] **Step 6: Commit**

```bash
git add cloud/internal/enroll/issuer.go cloud/internal/enroll/issuer_test.go cloud/go.mod cloud/go.sum
git commit -m "enroll: mint a device identity, or leave nothing behind"
```

---

### Task 5: The three routes

**Files:**
- Create: `cloud/cmd/enroll/handler.go`, `cloud/cmd/enroll/main.go`
- Test: `cloud/cmd/enroll/handler_test.go`
- Modify: `cloud/cmd/api/handler.go` (delete the `POST /api/devices/claim` case), `cloud/cmd/api/handler_test.go` (delete its claim tests), `Makefile`

**Interfaces:**
- Consumes: everything from Tasks 1–4, plus `devices.Store` (`Register`, `Claim`) from the existing package.
- Produces: `Handler` struct with fields `Enrollments enroll.Store`, `Devices devices.Store`, `Issuer enroll.Issuer`, `IoTEndpoint string`, `TTL time.Duration`, `CodeTTL time.Duration`; `Handle(ctx, events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error)`.

**Route ordering in the claim, which is the part to get right.** Reserve first, then issue, then record:

1. `ByCodeHash` — 404 if absent.
2. **The owner check.** If the enrollment carries an owner hint, compare it against the caller's `email`/`cognito:username` claim and 404 on a mismatch. This is what makes a code read off the screen useless to anybody but its owner, and it runs before the one-time guard so a stranger's failed attempt does not consume the claim.
3. `Reserve` — the one-time guard, before any AWS call. A lost race costs nothing because nothing has been minted.
4. `Issue` — mints the certificate, thing and policy attachment, cleaning up after itself on partial failure.
5. `devices.Register` then `devices.Claim` — the ownership row.
6. `SetCertificate` — the device may now collect.

If step 3 or 4 fails the owner ends up with a panel that never connects, which is exactly the recovery case §7.1 of the spec describes: unbind, which revokes, and the panel enrolls afresh.

- [ ] **Step 1: Write the failing tests**

Create `cloud/cmd/enroll/handler_test.go`:

```go
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/enroll"
)

func csrPEM(t *testing.T) string {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: "ignored"},
	}, key)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}))
}

func newHandler() (*Handler, *enroll.FakeIssuer) {
	issuer := &enroll.FakeIssuer{CertPEM: "CERTPEM"}
	return &Handler{
		Enrollments: enroll.NewFake(),
		Devices:     devices.NewFake(),
		Issuer:      issuer,
		IoTEndpoint: "a1.iot.us-east-1.amazonaws.com",
		TTL:         24 * time.Hour,
		CodeTTL:     15 * time.Minute,
	}, issuer
}

func post(path, body string) events.APIGatewayV2HTTPRequest {
	return events.APIGatewayV2HTTPRequest{
		RouteKey: "POST " + path,
		Body:     body,
	}
}

func submit(t *testing.T, h *Handler) (code, token string) {
	t.Helper()
	return submitFor(t, h, "")
}

// submitFor enrolls with an optional owner hint, the way a card prepared by
// the site would.
func submitFor(t *testing.T, h *Handler, owner string) (code, token string) {
	t.Helper()
	payload := `{"csr":` + strconv.Quote(csrPEM(t))
	if owner != "" {
		payload += `,"owner":` + strconv.Quote(owner)
	}
	payload += `}`
	resp, err := h.Handle(context.Background(), post("/api/enroll", payload))
	if err != nil || resp.StatusCode != 201 {
		t.Fatalf("submit: status %d err %v body %s", resp.StatusCode, err, resp.Body)
	}
	var out struct {
		Code  string `json:"code"`
		Token string `json:"token"`
	}
	if err := json.Unmarshal([]byte(resp.Body), &out); err != nil {
		t.Fatal(err)
	}
	return out.Code, out.Token
}

func claim(h *Handler, code, sub string) events.APIGatewayV2HTTPResponse {
	req := post("/api/devices/claim", `{"code":"`+code+`"}`)
	req.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{
			Claims: map[string]string{"sub": sub, "email": sub + "@example.com"},
		},
	}
	resp, _ := h.Handle(context.Background(), req)
	return resp
}

func collect(h *Handler, token string) events.APIGatewayV2HTTPResponse {
	req := events.APIGatewayV2HTTPRequest{
		RouteKey: "GET /api/enroll",
		Headers:  map[string]string{"authorization": "Bearer " + token},
	}
	resp, _ := h.Handle(context.Background(), req)
	return resp
}

func TestSubmitReturnsACodeAndAToken(t *testing.T) {
	h, issuer := newHandler()
	code, token := submit(t, h)
	if len(code) != 8 || token == "" {
		t.Fatalf("code %q token %q", code, token)
	}
	// The whole invariant: submitting mints nothing.
	if len(issuer.Calls) != 0 {
		t.Errorf("a certificate was issued before anyone claimed: %v", issuer.Calls)
	}
}

func TestSubmitRejectsRubbish(t *testing.T) {
	h, _ := newHandler()
	for name, body := range map[string]string{
		"not json":    `{`,
		"no csr":      `{}`,
		"not a csr":   `{"csr":"hello"}`,
	} {
		resp, _ := h.Handle(context.Background(), post("/api/enroll", body))
		if resp.StatusCode != 400 {
			t.Errorf("%s returned %d, want 400", name, resp.StatusCode)
		}
	}
}

func TestCollectingBeforeAnyoneClaimsIsAccepted(t *testing.T) {
	h, _ := newHandler()
	_, token := submit(t, h)
	if got := collect(h, token).StatusCode; got != 202 {
		t.Errorf("collect before claim returned %d, want 202", got)
	}
}

func TestTheWholeLoop(t *testing.T) {
	h, issuer := newHandler()
	code, token := submit(t, h)

	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Fatalf("claim returned %d", got)
	}
	if len(issuer.Calls) != 1 {
		t.Fatalf("issuer was called %d times", len(issuer.Calls))
	}

	resp := collect(h, token)
	if resp.StatusCode != 200 {
		t.Fatalf("collect returned %d: %s", resp.StatusCode, resp.Body)
	}
	var out struct {
		CertificatePem string `json:"certificatePem"`
		ThingName      string `json:"thingName"`
		Endpoint       string `json:"endpoint"`
	}
	if err := json.Unmarshal([]byte(resp.Body), &out); err != nil {
		t.Fatal(err)
	}
	if out.CertificatePem != "CERTPEM" || !strings.HasPrefix(out.ThingName, "scoreboard-") || out.Endpoint == "" {
		t.Errorf("collect returned %+v", out)
	}
}

func TestCollectingTwiceFails(t *testing.T) {
	// The row is deleted on collection. After that the certificate exists only
	// on the device and in AWS IoT.
	h, _ := newHandler()
	code, token := submit(t, h)
	claim(h, code, "owner-1")
	if got := collect(h, token).StatusCode; got != 200 {
		t.Fatal("first collect failed")
	}
	if got := collect(h, token).StatusCode; got != 404 {
		t.Errorf("second collect returned %d, want 404", got)
	}
}

func TestAWrongTokenIs404NotForbidden(t *testing.T) {
	// A wrong token and a nonexistent one must look the same, so nobody can
	// probe for which tokens exist.
	h, _ := newHandler()
	submit(t, h)
	if got := collect(h, "definitely-not-a-token").StatusCode; got != 404 {
		t.Errorf("got %d, want 404", got)
	}
}

func TestClaimingRequiresAuthentication(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submit(t, h)
	resp, _ := h.Handle(context.Background(), post("/api/devices/claim", `{"code":"`+code+`"}`))
	if resp.StatusCode != 401 {
		t.Errorf("unauthenticated claim returned %d, want 401", resp.StatusCode)
	}
	if len(issuer.Calls) != 0 {
		t.Error("an unauthenticated caller caused a certificate to be issued")
	}
}

func TestClaimingTwiceFails(t *testing.T) {
	h, issuer := newHandler()
	code, _ := submit(t, h)
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Fatal("first claim failed")
	}
	if got := claim(h, code, "owner-2").StatusCode; got != 404 {
		t.Errorf("second claim returned %d, want 404", got)
	}
	if len(issuer.Calls) != 1 {
		t.Errorf("issuer was called %d times; the second claim must not mint", len(issuer.Calls))
	}
}

func TestClaimingAnUnknownCodeIs404(t *testing.T) {
	h, _ := newHandler()
	if got := claim(h, "ZZZZ2222", "owner-1").StatusCode; got != 404 {
		t.Errorf("got %d, want 404", got)
	}
}

func TestAPreBoundPanelRefusesEverybodyElse(t *testing.T) {
	// The shoulder-surfing case, and the reason the owner hint exists. Somebody
	// who reads the code off the screen has an invited account of their own --
	// and still cannot use what they saw.
	h, issuer := newHandler()
	code, _ := submitFor(t, h, "owner-1@example.com")

	if got := claim(h, code, "owner-2").StatusCode; got != 404 {
		t.Errorf("a stranger's claim returned %d, want 404", got)
	}
	if len(issuer.Calls) != 0 {
		t.Fatal("a stranger's claim minted a certificate")
	}
	// And the real owner is not locked out by the failed attempt: the one-time
	// guard must not have been consumed.
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Errorf("the owner's claim returned %d, want 200", got)
	}
}

func TestAPreBoundPanelMatchesItsOwnerLoosely(t *testing.T) {
	// The hint is typed into a file by hand; the claim arrives in a JWT. Case
	// and whitespace must not decide who owns a panel.
	h, _ := newHandler()
	code, _ := submitFor(t, h, "  Owner-1@Example.COM ")
	if got := claim(h, code, "owner-1").StatusCode; got != 200 {
		t.Errorf("the owner's claim returned %d, want 200", got)
	}
}

func TestAnUnboundPanelIsClaimableByAnyInvitedUser(t *testing.T) {
	// A card prepared without a setup file still works. The hint is optional,
	// not a new way to brick an install.
	h, _ := newHandler()
	code, _ := submit(t, h)
	if got := claim(h, code, "anybody").StatusCode; got != 200 {
		t.Errorf("got %d, want 200", got)
	}
}

func TestCodesAreAcceptedAsPeopleTypeThem(t *testing.T) {
	h, _ := newHandler()
	code, _ := submit(t, h)
	typed := strings.ToLower(code[:4] + "-" + code[4:])
	if got := claim(h, typed, "owner-1").StatusCode; got != 200 {
		t.Errorf("claim with %q returned %d", typed, got)
	}
}
```

Add `"strconv"` to that file's imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd cloud && go test ./cmd/enroll/ 2>&1 | head -10`
Expected: FAIL — the package does not exist.

- [ ] **Step 3: Write the handler**

Create `cloud/cmd/enroll/handler.go`:

```go
// Command enroll turns a freshly flashed panel into a registered device.
//
// It is a separate Lambda from the admin API on purpose. Minting a device
// identity needs four AWS IoT actions the admin API neither has nor should
// have: every route there is reachable by any signed-in user, and putting
// fleet-identity creation behind that credential would widen the blast radius
// of every bug in it.
package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/enroll"
)

type Handler struct {
	Enrollments enroll.Store
	Devices     devices.Store
	Issuer      enroll.Issuer
	IoTEndpoint string
	TTL         time.Duration // how long an enrollment lives: a day
	CodeTTL     time.Duration // how long one pairing code lives: minutes
}

func respond(status int, body any) (events.APIGatewayV2HTTPResponse, error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return events.APIGatewayV2HTTPResponse{StatusCode: 500}, nil
	}
	return events.APIGatewayV2HTTPResponse{
		StatusCode: status,
		Headers:    map[string]string{"content-type": "application/json"},
		Body:       string(payload),
	}, nil
}

func fail(status int, msg string) (events.APIGatewayV2HTTPResponse, error) {
	return respond(status, map[string]string{"error": msg})
}

func subject(req events.APIGatewayV2HTTPRequest) string {
	if req.RequestContext.Authorizer == nil || req.RequestContext.Authorizer.JWT == nil {
		return ""
	}
	return req.RequestContext.Authorizer.JWT.Claims["sub"]
}

func body(req events.APIGatewayV2HTTPRequest) []byte {
	if !req.IsBase64Encoded {
		return []byte(req.Body)
	}
	raw, err := base64.StdEncoding.DecodeString(req.Body)
	if err != nil {
		return nil
	}
	return raw
}

// bearer returns the collection token a device presents. It is not a JWT and
// no authorizer validates it; this process looks it up, and a token that does
// not resolve is answered 404 exactly like one that never existed.
func bearer(req events.APIGatewayV2HTTPRequest) string {
	for name, value := range req.Headers {
		if strings.EqualFold(name, "authorization") {
			return strings.TrimSpace(strings.TrimPrefix(value, "Bearer "))
		}
	}
	return ""
}

func (h *Handler) Handle(ctx context.Context, req events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
	switch req.RouteKey {
	case "POST /api/enroll":
		return h.submit(ctx, req)
	case "GET /api/enroll":
		return h.collect(ctx, req)
	case "POST /api/devices/claim":
		return h.claim(ctx, req)
	default:
		return fail(http.StatusNotFound, "no such route")
	}
}

// submit is the only unauthenticated route in this project. It can create a
// pending row and nothing else: no thing, no certificate, no policy
// attachment. Everything that costs money or grants access waits for claim.
func (h *Handler) submit(ctx context.Context, req events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
	var in struct {
		CSR   string `json:"csr"`
		Owner string `json:"owner"` // optional; from the card's setup file
	}
	if err := json.Unmarshal(body(req), &in); err != nil || in.CSR == "" {
		return fail(http.StatusBadRequest, "a certificate request is required")
	}
	if err := enroll.ParseCSR([]byte(in.CSR)); err != nil {
		return fail(http.StatusBadRequest, err.Error())
	}
	code, err := enroll.Code()
	if err != nil {
		return fail(http.StatusInternalServerError, "enrollment failed")
	}
	token, err := enroll.Token()
	if err != nil {
		return fail(http.StatusInternalServerError, "enrollment failed")
	}
	thingName, err := enroll.ThingName()
	if err != nil {
		return fail(http.StatusInternalServerError, "enrollment failed")
	}
	pending := enroll.Pending{
		TokenHash:     enroll.HashSecret(token),
		CodeHash:      enroll.HashSecret(code),
		CodeExpiresAt: time.Now().Add(h.CodeTTL).Unix(),
		CSR:           in.CSR,
		ThingName:     thingName,
		ExpiresAt:     time.Now().Add(h.TTL).Unix(),
	}
	// An owner hint binds this enrollment to one person, so the code on the
	// screen is useless to anybody else. Absent is fine: the panel then
	// behaves as an open enrollment with short rotating codes.
	if hint := enroll.NormalizeOwner(in.Owner); hint != "" {
		pending.OwnerHintHash = enroll.HashSecret(hint)
	}
	if err := h.Enrollments.Create(ctx, pending); err != nil {
		// A duplicate code is not the caller's problem; try again with another.
		if errors.Is(err, enroll.ErrCodeTaken) {
			return fail(http.StatusConflict, "please retry")
		}
		slog.Error("creating enrollment", "err", err)
		return fail(http.StatusInternalServerError, "enrollment failed")
	}
	// The raw code and token are returned once, here, and never stored.
	return respond(http.StatusCreated, map[string]any{
		"code":         code,
		"display":      enroll.FormatCode(code),
		"token":        token,
		"pollSeconds":  5,
	})
}

// collect hands the certificate to the device that asked for it, once.
func (h *Handler) collect(ctx context.Context, req events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
	token := bearer(req)
	if token == "" {
		return fail(http.StatusNotFound, "no such enrollment")
	}
	p, found, err := h.Enrollments.ByTokenHash(ctx, enroll.HashSecret(token))
	if err != nil {
		slog.Error("reading enrollment", "err", err)
		return fail(http.StatusInternalServerError, "enrollment lookup failed")
	}
	// 404 rather than 403: a wrong token and a nonexistent one must look the
	// same, so nobody can probe for which tokens exist.
	if !found {
		return fail(http.StatusNotFound, "no such enrollment")
	}
	if p.Status != enroll.StatusReady {
		// The poll is what rotates the code: the panel updates its own display
		// without needing a second endpoint, and a code photographed off a
		// screen stops working within the quarter hour.
		code, expires := "", p.CodeExpiresAt
		if time.Now().Unix() >= p.CodeExpiresAt {
			fresh, err := h.rotate(ctx, p)
			if err != nil {
				slog.Error("rotating code", "err", err)
			} else {
				code, expires = fresh, time.Now().Add(h.CodeTTL).Unix()
			}
		}
		out := map[string]any{"status": "waiting to be claimed", "codeExpiresAt": expires}
		if code != "" {
			out["code"], out["display"] = code, enroll.FormatCode(code)
		}
		return respond(http.StatusAccepted, out)
	}
	out := map[string]string{
		"certificatePem": p.CertPEM,
		"thingName":      p.ThingName,
		"endpoint":       h.IoTEndpoint,
	}
	// Delete before responding: if the delete fails we would rather the device
	// retry than leave a certificate collectable twice.
	if err := h.Enrollments.Delete(ctx, p.TokenHash); err != nil {
		slog.Error("deleting collected enrollment", "err", err)
		return fail(http.StatusInternalServerError, "enrollment cleanup failed")
	}
	return respond(http.StatusOK, out)
}

// rotate mints a fresh code for a waiting enrollment. A collision means
// somebody else holds that code; try again rather than handing out a duplicate.
func (h *Handler) rotate(ctx context.Context, p enroll.Pending) (string, error) {
	var lastErr error
	for attempt := 0; attempt < 5; attempt++ {
		code, err := enroll.Code()
		if err != nil {
			return "", err
		}
		err = h.Enrollments.RotateCode(ctx, p.TokenHash, enroll.HashSecret(code), time.Now().Add(h.CodeTTL).Unix())
		if err == nil {
			return code, nil
		}
		if !errors.Is(err, enroll.ErrCodeTaken) {
			return "", err
		}
		lastErr = err
	}
	return "", lastErr
}

// ownerMatches decides whether this caller may claim this enrollment. An
// enrollment with no hint may be claimed by any invited user; one with a hint
// may be claimed only by the person the card named.
func ownerMatches(p enroll.Pending, req events.APIGatewayV2HTTPRequest) bool {
	if p.OwnerHintHash == "" {
		return true
	}
	if req.RequestContext.Authorizer == nil || req.RequestContext.Authorizer.JWT == nil {
		return false
	}
	claims := req.RequestContext.Authorizer.JWT.Claims
	// sub is a UUID nobody could have typed into a file, so the comparison is
	// against what a person would actually write.
	for _, claim := range []string{claims["email"], claims["cognito:username"]} {
		if claim == "" {
			continue
		}
		if enroll.HashSecret(enroll.NormalizeOwner(claim)) == p.OwnerHintHash {
			return true
		}
	}
	return false
}

// claim binds a pending enrollment to the signed-in caller and mints the
// certificate. Reserve runs first, before any AWS call, so two people racing
// the same code cannot both mint one.
func (h *Handler) claim(ctx context.Context, req events.APIGatewayV2HTTPRequest) (events.APIGatewayV2HTTPResponse, error) {
	sub := subject(req)
	if sub == "" {
		return fail(http.StatusUnauthorized, "unauthenticated")
	}
	var in struct {
		Code string `json:"code"`
	}
	if err := json.Unmarshal(body(req), &in); err != nil || in.Code == "" {
		return fail(http.StatusBadRequest, "a code is required")
	}
	code := enroll.NormalizeCode(in.Code)
	p, found, err := h.Enrollments.ByCodeHash(ctx, enroll.HashSecret(code))
	if err != nil {
		slog.Error("reading enrollment by code", "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	if !found {
		return fail(http.StatusNotFound, "no enrollment with that code")
	}
	// 404, not 403: somebody who read the code off a screen learns nothing
	// about whether it was real. Checked before Reserve, so a stranger's
	// attempt cannot consume the one-time claim.
	if !ownerMatches(p, req) {
		return fail(http.StatusNotFound, "no enrollment with that code")
	}
	if err := h.Enrollments.Reserve(ctx, p.TokenHash, sub); err != nil {
		if errors.Is(err, enroll.ErrAlreadyClaimed) || errors.Is(err, enroll.ErrNotFound) {
			return fail(http.StatusNotFound, "no enrollment with that code")
		}
		slog.Error("reserving enrollment", "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	certPEM, err := h.Issuer.Issue(ctx, p.CSR, p.ThingName)
	if err != nil {
		slog.Error("issuing certificate", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	if err := h.Devices.Register(ctx, p.ThingName, code); err != nil {
		slog.Error("registering device", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	if err := h.Devices.Claim(ctx, p.ThingName, sub); err != nil {
		slog.Error("claiming device", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	if err := h.Enrollments.SetCertificate(ctx, p.TokenHash, certPEM); err != nil {
		slog.Error("storing certificate", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	return respond(http.StatusOK, map[string]string{"thingName": p.ThingName})
}
```

- [ ] **Step 4: Write the Lambda wiring**

Create `cloud/cmd/enroll/main.go`:

```go
package main

import (
	"context"
	"log/slog"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iot"

	"hockeytrack-scoreboard/internal/devices"
	"hockeytrack-scoreboard/internal/enroll"
)

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	enrollments := os.Getenv("ENROLLMENTS_TABLE")
	deviceTable := os.Getenv("DEVICES_TABLE")
	endpoint := os.Getenv("IOT_ENDPOINT")
	policy := os.Getenv("DEVICE_POLICY")
	if enrollments == "" || deviceTable == "" || endpoint == "" || policy == "" {
		slog.Error("ENROLLMENTS_TABLE, DEVICES_TABLE, IOT_ENDPOINT and DEVICE_POLICY are required")
		os.Exit(1)
	}
	ddb := dynamodb.NewFromConfig(cfg)
	h := &Handler{
		Enrollments: enroll.NewDynamo(ddb, enrollments),
		Devices:     devices.NewDynamo(ddb, deviceTable),
		Issuer:      enroll.NewIoTIssuer(iot.NewFromConfig(cfg), policy),
		IoTEndpoint: endpoint,
		TTL:         24 * time.Hour,
		// The spec's two lifetimes: an enrollment lasts a day so a panel need
		// not regenerate a keypair because nobody was home, while a code lasts
		// a quarter hour because it is displayed on a screen.
		CodeTTL: 15 * time.Minute,
	}
	lambda.Start(h.Handle)
}
```

- [ ] **Step 5: Remove the claim route from the admin API**

In `cloud/cmd/api/handler.go`, delete the whole `case "POST /api/devices/claim":` block. Claiming now happens where the minting permissions live, and leaving a second implementation behind would be a second thing to keep correct.

Delete the corresponding tests from `cloud/cmd/api/handler_test.go`. `devices.Store.ByCode` stays — it is still used by the store's own tests and the `Fake` — but nothing in `cmd/api` calls it any more.

- [ ] **Step 6: Add the binary to the build**

In `Makefile`, alongside the existing `build/api` lines:

```make
	cd cloud && CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -ldflags="-s -w" -o ../build/enroll/bootstrap ./cmd/enroll
	cd build/enroll && python3 -m zipfile -c ../enroll.zip bootstrap
```

and add `build/enroll` to the `mkdir -p` line.

- [ ] **Step 7: Run everything**

Run: `cd cloud && go vet ./... && go test ./... 2>&1 | tail -10`
Expected: PASS, including the admin API tests with the claim tests removed.

- [ ] **Step 8: Commit**

```bash
git add cloud/cmd/enroll cloud/cmd/api/handler.go cloud/cmd/api/handler_test.go Makefile
git commit -m "enroll: submit, claim and collect, in a Lambda of their own"
```

---

### Task 6: The infrastructure

**Files:**
- Create: `terraform/enroll.tf`
- Modify: `terraform/admin.tf` (the claim route's integration moves to the new Lambda), `terraform/iot-alarms.tf` (the retained-publish runbook names a fourth identity)

**Interfaces:**
- Consumes: the Lambda built by Task 5's Makefile change (`build/enroll.zip`), and the environment variables its `main.go` requires: `ENROLLMENTS_TABLE`, `DEVICES_TABLE`, `IOT_ENDPOINT`, `DEVICE_POLICY`.
- Produces: `aws_dynamodb_table.enrollments`, `aws_lambda_function.enroll`, `aws_iam_role.enroll`, three routes on the existing HTTP API, per-route throttling, and two alarms.

**One table, no secondary index.** The code reservation from Task 3 lives in the same table under a `code#` prefix, so a code lookup is a `GetItem` rather than a query. TTL is on `expiresAt`, which both item kinds carry, so expiry costs nothing and applies to abandoned codes as well as abandoned enrollments.

- [ ] **Step 1: Write the table and the role**

Create `terraform/enroll.tf`:

```hcl
# Device enrollment: how a freshly flashed panel earns a certificate.
# Spec: docs/superpowers/specs/2026-09-12-device-enrollment-design.md

# One table, two kinds of item. An enrollment is keyed by the hash of its
# collection token; a code reservation is keyed by "code#" plus the hash of the
# code and points back at the enrollment. The reservation is what makes codes
# unique -- DynamoDB cannot enforce uniqueness on an index, but it can refuse a
# conditional write -- and it removes the need for an index at all.
#
# Deliberately NOT the scoreboard-devices table. That one is the irreplaceable
# ownership record (SCO-23); anonymous internet traffic has no business
# churning writes into it.
resource "aws_dynamodb_table" "enrollments" {
  name         = "scoreboard-enrollments"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Expiry is the database's job. Enrollments live a day, code reservations
  # fifteen minutes, and both carry expiresAt.
  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_iam_role" "enroll" {
  name = "scoreboard-enroll"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "enroll" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.enroll.arn}:*"]
  }

  statement {
    sid = "Enrollments"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ]
    resources = [aws_dynamodb_table.enrollments.arn]
  }

  # The ownership row, written once at claim. No DeleteItem: this function has
  # no reason to remove a device somebody owns.
  statement {
    sid       = "OwnershipRow"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.devices.arn]
  }

  # Minting a device identity. CreateCertificateFromCsr cannot be scoped to a
  # resource that does not exist yet, so the containment comes from the next
  # statement and from this role being reachable by nothing but this function.
  statement {
    sid       = "MintCertificate"
    actions   = ["iot:CreateCertificateFromCsr"]
    resources = ["*"]
  }

  statement {
    sid = "RegisterThing"
    actions = [
      "iot:CreateThing",
      "iot:DeleteThing",
      "iot:AttachThingPrincipal",
      "iot:DetachThingPrincipal",
      "iot:UpdateCertificate",
      "iot:DeleteCertificate",
    ]
    resources = [
      "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:thing/scoreboard-*",
      "arn:aws:iot:${var.region}:${data.aws_caller_identity.current.account_id}:cert/*",
    ]
  }

  # THE constraint. Unpinned, a bug in device onboarding becomes fleet-wide
  # compromise: mint a certificate, attach an over-permissive policy, and the
  # result is a credential nobody intended to exist. Pinned to one policy, the
  # worst an enrollment bug produces is another ordinary scoreboard.
  statement {
    sid       = "AttachOnlyTheDevicePolicy"
    actions   = ["iot:AttachPolicy", "iot:DetachPolicy"]
    resources = [aws_iot_policy.device.arn]
  }
}

resource "aws_iam_role_policy" "enroll" {
  name   = "scoreboard-enroll"
  role   = aws_iam_role.enroll.id
  policy = data.aws_iam_policy_document.enroll.json
}

resource "aws_cloudwatch_log_group" "enroll" {
  name              = "/aws/lambda/scoreboard-enroll"
  retention_in_days = 30
}

resource "aws_lambda_function" "enroll" {
  function_name    = "scoreboard-enroll"
  role             = aws_iam_role.enroll.arn
  filename         = "${path.module}/../build/enroll.zip"
  source_code_hash = filebase64sha256("${path.module}/../build/enroll.zip")
  handler          = "bootstrap"
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  timeout          = 15

  environment {
    variables = {
      ENROLLMENTS_TABLE = aws_dynamodb_table.enrollments.name
      DEVICES_TABLE     = aws_dynamodb_table.devices.name
      IOT_ENDPOINT      = data.aws_iot_endpoint.data.endpoint_address
      DEVICE_POLICY     = aws_iot_policy.device.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.enroll]
}
```

Check the exact names of `aws_dynamodb_table.devices`, `aws_iot_policy.device` and the IoT endpoint data source against `terraform/admin.tf` and `terraform/iot.tf` before writing this, and use whatever those files actually call them.

- [ ] **Step 2: Wire the routes**

Append to `terraform/enroll.tf`:

```hcl
resource "aws_apigatewayv2_integration" "enroll" {
  api_id                 = aws_apigatewayv2_api.admin.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.enroll.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_lambda_permission" "enroll_api" {
  statement_id  = "AllowAPIGatewayInvokeEnroll"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.enroll.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.admin.execution_arn}/*/*"
}

# Two of these are deliberately unauthenticated -- the only such routes in the
# project. A caller who reaches them can create a pending row and nothing else:
# no thing, no certificate, no policy attachment. Everything that grants access
# waits for POST /api/devices/claim, which the JWT authorizer guards.
resource "aws_apigatewayv2_route" "enroll_submit" {
  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = "POST /api/enroll"
  target             = "integrations/${aws_apigatewayv2_integration.enroll.id}"
  authorization_type = "NONE"
}

# The device's own bearer token is checked in the function, not by an
# authorizer: it is not a JWT, and a token that does not resolve is answered
# 404 exactly like one that never existed.
resource "aws_apigatewayv2_route" "enroll_collect" {
  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = "GET /api/enroll"
  target             = "integrations/${aws_apigatewayv2_integration.enroll.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_route" "enroll_claim" {
  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = "POST /api/devices/claim"
  target             = "integrations/${aws_apigatewayv2_integration.enroll.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}
```

Then **delete the existing `POST /api/devices/claim` route from `terraform/admin.tf`**, since it now points at the wrong function. Check that file for the route's current resource name rather than guessing it.

- [ ] **Step 3: Throttle the unauthenticated routes separately**

In `terraform/admin.tf`, the `$default` stage already carries `default_route_settings` of 20 rps and 40 burst. Add per-route settings so enrollment traffic cannot starve the site:

```hcl
  route_settings {
    route_key              = "POST /api/enroll"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }

  route_settings {
    route_key              = "GET /api/enroll"
    throttling_rate_limit  = 5
    throttling_burst_limit = 10
  }
```

- [ ] **Step 4: Alarm on the two things that would mean trouble**

Append to `terraform/enroll.tf`. The spec chose detection over a WAF deliberately (§6), which only works if these exist:

```hcl
# Enrollment is a handful of requests a week in normal use: somebody sets up a
# panel. A sustained rate means either an abuser filling the table with junk
# rows or a fleet of panels stuck in a retry loop, and both are worth knowing
# about the same day rather than at the end of the month.
resource "aws_cloudwatch_metric_alarm" "enroll_flood" {
  alarm_name          = "scoreboard-enroll-flood"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 100
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.enroll.function_name }
  alarm_description   = <<-EOT
    More than 100 enrollment calls in five minutes. Normal traffic is a few a
    week. Check the enrollment table for junk rows and the function's logs for
    whether the calls are submits (an abuser) or collects (panels retrying).
    The deliberate position in the spec is that this is where a WAF gets added
    if it is ever needed -- per-IP limiting was judged not worth a standing
    monthly cost to defend against an attack whose payoff is a few dollars of
    DynamoDB writes. This alarm firing is the evidence that changes that.
  EOT
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}

# A claim that fails is somebody typing a code wrong, which happens. A lot of
# them is somebody guessing, and the codes are only eight characters.
resource "aws_cloudwatch_metric_alarm" "enroll_claim_failures" {
  alarm_name          = "scoreboard-enroll-claim-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 20
  period              = 300
  statistic           = "Sum"
  namespace           = "AWS/ApiGateway"
  metric_name         = "4xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.admin.id }
  alarm_description   = <<-EOT
    More than 20 4xx responses from the admin API in five minutes. The likely
    innocent cause is a panel polling with a stale token; the one worth acting
    on is somebody working through the pairing-code space. Check the access log
    group for which route and which source address.
  EOT
  alarm_actions       = [aws_sns_topic.alerts.arn]
  treat_missing_data  = "notBreaching"
}
```

Check what the alerts topic is actually called in this stack before writing `aws_sns_topic.alerts`; `terraform/iot-alarms.tf` already references it.

- [ ] **Step 5: Update the retained-publish runbook**

`terraform/iot-alarms.tf` has an alarm whose description names the identities permitted to publish. It currently names three; nothing changes about publishing here, but the enrollment function is a fourth principal that touches IoT, and the runbook should say so rather than leave a reader wondering. Add a sentence noting that `scoreboard-enroll` holds registration permissions but never publishes.

- [ ] **Step 6: Validate**

Run: `cd terraform && terraform fmt -check && terraform validate`
Expected: formatting clean, configuration valid.

Do **not** run `terraform plan` or `apply` — the plan needs `build/enroll.zip`, which means building, and applying is a separate human-approved step.

- [ ] **Step 7: Commit**

```bash
git add terraform/enroll.tf terraform/admin.tf terraform/iot-alarms.tf
git commit -m "terraform: a second Lambda, and the only role that can mint a device"
```
