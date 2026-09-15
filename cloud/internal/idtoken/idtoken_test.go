package idtoken

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const (
	testPool   = "us-east-1_TEST"
	testClient = "client-123"
	testIssuer = "https://cognito-idp.us-east-1.amazonaws.com/" + testPool
)

var (
	keysOnce               sync.Once
	signingKey, rotatedKey *rsa.PrivateKey
)

func keys(t *testing.T) (*rsa.PrivateKey, *rsa.PrivateKey) {
	t.Helper()
	keysOnce.Do(func() {
		var err error
		if signingKey, err = rsa.GenerateKey(rand.Reader, 2048); err != nil {
			panic(err)
		}
		if rotatedKey, err = rsa.GenerateKey(rand.Reader, 2048); err != nil {
			panic(err)
		}
	})
	return signingKey, rotatedKey
}

type jwk struct {
	Kty string `json:"kty"`
	Kid string `json:"kid"`
	Use string `json:"use,omitempty"`
	N   string `json:"n,omitempty"`
	E   string `json:"e,omitempty"`
}

func rsaJWK(kid string, pub *rsa.PublicKey) jwk {
	return jwk{
		Kty: "RSA", Kid: kid, Use: "sig",
		N: base64.RawURLEncoding.EncodeToString(pub.N.Bytes()),
		E: base64.RawURLEncoding.EncodeToString(big.NewInt(int64(pub.E)).Bytes()),
	}
}

// jwksServer serves a key set the test can change, and counts fetches.
type jwksServer struct {
	srv  *httptest.Server
	mu   sync.Mutex
	set  []jwk
	raw  []byte // when set, served instead of set
	fail bool
	hits int
}

func newJWKS(t *testing.T, set ...jwk) *jwksServer {
	t.Helper()
	j := &jwksServer{set: set}
	j.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		j.mu.Lock()
		defer j.mu.Unlock()
		j.hits++
		if j.fail {
			http.Error(w, "down", http.StatusServiceUnavailable)
			return
		}
		if j.raw != nil {
			_, _ = w.Write(j.raw)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string][]jwk{"keys": j.set})
	}))
	t.Cleanup(j.srv.Close)
	return j
}

func (j *jwksServer) fetches() int {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.hits
}

type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

func testVerifier(j *jwksServer, c *clock) *Verifier {
	v := New("us-east-1", testPool, testClient)
	v.jwksURL = j.srv.URL
	v.now = c.now
	return v
}

func goodClaims(now time.Time) jwt.MapClaims {
	return jwt.MapClaims{
		"sub":              "sub-1",
		"iss":              testIssuer,
		"aud":              testClient,
		"token_use":        "id",
		"exp":              now.Add(time.Hour).Unix(),
		"iat":              now.Unix(),
		"email":            "owner@example.com",
		"email_verified":   true,
		"cognito:username": "Google_123",
	}
}

func sign(t *testing.T, key *rsa.PrivateKey, kid string, claims jwt.MapClaims) string {
	t.Helper()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	tok.Header["kid"] = kid
	s, err := tok.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func setup(t *testing.T) (*Verifier, *jwksServer, *clock, *rsa.PrivateKey) {
	t.Helper()
	key, _ := keys(t)
	j := newJWKS(t, rsaJWK("k1", &key.PublicKey))
	c := &clock{t: time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)}
	return testVerifier(j, c), j, c, key
}

func TestAValidTokenIsAcceptedWithOrWithoutTheBearerScheme(t *testing.T) {
	v, _, c, key := setup(t)
	tok := sign(t, key, "k1", goodClaims(c.t))
	for _, header := range []string{"Bearer " + tok, "bearer " + tok, tok} {
		got, err := v.Verify(context.Background(), header)
		if err != nil {
			t.Fatalf("%.10q...: %v", header, err)
		}
		want := Claims{Sub: "sub-1", Email: "owner@example.com", EmailVerified: true, CognitoUsername: "Google_123"}
		if got != want {
			t.Errorf("claims = %+v, want %+v", got, want)
		}
	}
}

func TestForgedAndWrongTokensAreRefused(t *testing.T) {
	v, _, c, key := setup(t)
	_, other := keys(t)
	pubDER, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	pubPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: pubDER})

	with := func(edit func(jwt.MapClaims)) jwt.MapClaims {
		m := goodClaims(c.t)
		edit(m)
		return m
	}
	none, err := jwt.NewWithClaims(jwt.SigningMethodNone, goodClaims(c.t)).SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatal(err)
	}
	hsTok := jwt.NewWithClaims(jwt.SigningMethodHS256, goodClaims(c.t))
	hsTok.Header["kid"] = "k1"
	hs, err := hsTok.SignedString(pubPEM)
	if err != nil {
		t.Fatal(err)
	}

	cases := map[string]string{
		"alg none":                  "Bearer " + none,
		"HS256 with the public key": "Bearer " + hs,
		"signed by another key":     "Bearer " + sign(t, other, "k1", goodClaims(c.t)),
		"wrong issuer":              "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["iss"] = "https://attacker.example.com" })),
		"wrong audience":            "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["aud"] = "someone-else" })),
		"extra audience":            "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["aud"] = []string{testClient, "someone-else"} })),
		"access token":              "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["token_use"] = "access" })),
		"no exp":                    "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { delete(m, "exp") })),
		"expired beyond leeway":     "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { m["exp"] = c.t.Add(-31 * time.Second).Unix() })),
		"no sub":                    "Bearer " + sign(t, key, "k1", with(func(m jwt.MapClaims) { delete(m, "sub") })),
		"no kid":                    "Bearer " + sign(t, key, "", goodClaims(c.t)),
		"empty header":              "",
		"basic scheme":              "Basic dXNlcjpwYXNz",
		"garbage":                   "Bearer not-a-token",
	}
	for name, header := range cases {
		_, err := v.Verify(context.Background(), header)
		if !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: err = %v, want ErrInvalid", name, err)
		}
	}
}

func TestExpiryAllowsThirtySecondsOfClockDrift(t *testing.T) {
	v, _, c, key := setup(t)
	m := goodClaims(c.t)
	m["exp"] = c.t.Add(-29 * time.Second).Unix()
	if _, err := v.Verify(context.Background(), sign(t, key, "k1", m)); err != nil {
		t.Errorf("a token 29s past exp was refused: %v", err)
	}
}

func TestEmailVerifiedAcceptsOnlyTrueOrTheStringTrue(t *testing.T) {
	v, _, c, key := setup(t)
	for raw, want := range map[any]bool{true: true, "true": true, false: false, "false": false, "yes": false, 1: false} {
		m := goodClaims(c.t)
		m["email_verified"] = raw
		got, err := v.Verify(context.Background(), sign(t, key, "k1", m))
		if err != nil {
			t.Fatalf("%v: %v", raw, err)
		}
		if got.EmailVerified != want {
			t.Errorf("email_verified %#v -> %v, want %v", raw, got.EmailVerified, want)
		}
	}
	m := goodClaims(c.t)
	delete(m, "email_verified")
	if got, _ := v.Verify(context.Background(), sign(t, key, "k1", m)); got.EmailVerified {
		t.Error("an absent email_verified was treated as verified")
	}
}

func TestAnUnknownKeyIDRefetchesAtMostEveryFiveMinutes(t *testing.T) {
	v, j, c, key := setup(t)
	_, rotated := keys(t)
	ctx := context.Background()

	if _, err := v.Verify(ctx, sign(t, key, "k1", goodClaims(c.t))); err != nil {
		t.Fatal(err)
	}
	if j.fetches() != 1 {
		t.Fatalf("fetches = %d after first use, want 1", j.fetches())
	}

	// Cognito rotates, but inside the window the verifier does not ask again.
	j.mu.Lock()
	j.set = append(j.set, rsaJWK("k2", &rotated.PublicKey))
	j.mu.Unlock()
	rotatedTok := sign(t, rotated, "k2", goodClaims(c.t))
	if _, err := v.Verify(ctx, rotatedTok); !errors.Is(err, ErrInvalid) {
		t.Fatalf("unknown kid inside the window: err = %v, want ErrInvalid", err)
	}
	if _, err := v.Verify(ctx, sign(t, key, "junk", goodClaims(c.t))); !errors.Is(err, ErrInvalid) {
		t.Fatalf("second unknown kid: err = %v, want ErrInvalid", err)
	}
	if j.fetches() != 1 {
		t.Fatalf("fetches = %d inside the window, want 1", j.fetches())
	}

	// Five minutes on, one refetch picks up the rotated key.
	c.t = c.t.Add(5 * time.Minute)
	if _, err := v.Verify(ctx, rotatedTok); err != nil {
		t.Fatalf("rotated key after the window: %v", err)
	}
	if j.fetches() != 2 {
		t.Fatalf("fetches = %d after the window, want 2", j.fetches())
	}
	if _, err := v.Verify(ctx, sign(t, key, "junk", goodClaims(c.t))); !errors.Is(err, ErrInvalid) {
		t.Fatalf("junk kid right after a refetch: err = %v, want ErrInvalid", err)
	}
	if j.fetches() != 2 {
		t.Errorf("fetches = %d, want 2: a junk kid right after a refetch must not fetch again", j.fetches())
	}
}

func TestAKeyFetchFailureIsUnavailableNotInvalid(t *testing.T) {
	v, j, c, key := setup(t)
	tok := sign(t, key, "k1", goodClaims(c.t))
	j.mu.Lock()
	j.fail = true
	j.mu.Unlock()
	if _, err := v.Verify(context.Background(), tok); !errors.Is(err, ErrUnavailable) || errors.Is(err, ErrInvalid) {
		t.Fatalf("err = %v, want ErrUnavailable only", err)
	}
	j.mu.Lock()
	j.fail = false
	j.mu.Unlock()
	if _, err := v.Verify(context.Background(), tok); err != nil {
		t.Errorf("after Cognito recovered: %v", err)
	}
}

func TestAnOversizedKeySetIsUnavailable(t *testing.T) {
	v, j, c, key := setup(t)
	j.mu.Lock()
	j.raw = []byte(`{"keys":[],"pad":"` + strings.Repeat("a", maxJWKSBytes) + `"}`)
	j.mu.Unlock()
	if _, err := v.Verify(context.Background(), sign(t, key, "k1", goodClaims(c.t))); !errors.Is(err, ErrUnavailable) {
		t.Errorf("err = %v, want ErrUnavailable", err)
	}
}

func TestOnlyRSASigningKeysOfAtLeast2048BitsAreKept(t *testing.T) {
	key, _ := keys(t)
	small, err := rsa.GenerateKey(rand.Reader, 1024)
	if err != nil {
		t.Fatal(err)
	}
	enc := rsaJWK("enc", &key.PublicKey)
	enc.Use = "enc"
	noUse := rsaJWK("nouse", &key.PublicKey)
	noUse.Use = ""
	body, _ := json.Marshal(map[string][]jwk{"keys": {
		rsaJWK("good", &key.PublicKey),
		noUse,
		enc,
		{Kty: "EC", Kid: "ec"},
		rsaJWK("small", &small.PublicKey),
	}})
	got, err := parseJWKS(body)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got["good"] == nil || got["nouse"] == nil {
		t.Errorf("kept %v, want exactly good and nouse", mapKeys(got))
	}
	if _, err := parseJWKS([]byte(`{"keys":[{"kty":"EC","kid":"ec"}]}`)); err == nil {
		t.Error("a key set with no usable key parsed without error")
	}
}

func mapKeys(m map[string]*rsa.PublicKey) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
