// Package idtoken verifies the Cognito ID tokens the admin site sends, inside
// the function that acts on them.
//
// API Gateway's JWT authorizer already checks each token and hands the
// function its claims in the event. The function cannot tell that event from
// one somebody built by hand and sent with lambda:Invoke, skipping API Gateway
// entirely, so identity is taken from the token itself, checked here, and
// never from the authorizer's claims.
package idtoken

import (
	"context"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

var (
	// ErrInvalid means the caller did not present a token this pool issued
	// to this client that is still in date. Answer 401.
	ErrInvalid = errors.New("idtoken: invalid token")
	// ErrUnavailable means the pool's signing keys could not be fetched, so
	// no token can be checked. Answer 503: it fails closed without blaming
	// the caller.
	ErrUnavailable = errors.New("idtoken: signing keys unavailable")
)

const (
	leeway          = 30 * time.Second
	refetchInterval = 5 * time.Minute
	fetchTimeout    = 5 * time.Second
	maxJWKSBytes    = 64 << 10
	minKeyBits      = 2048
)

// Claims is what the handlers may know about a verified caller.
type Claims struct {
	Sub             string
	Email           string
	EmailVerified   bool
	CognitoUsername string
}

// Tokens verifies an Authorization header. Verifier is the real one; tests
// use idtokentest.Fake.
type Tokens interface {
	Verify(ctx context.Context, authorizationHeader string) (Claims, error)
}

// Verifier checks tokens against one user pool and one app client.
type Verifier struct {
	issuer   string
	audience string
	jwksURL  string
	client   *http.Client
	now      func() time.Time

	mu        sync.Mutex
	keys      map[string]*rsa.PublicKey
	fetchedAt time.Time
}

// New returns a Verifier for tokens the pool issues to the client. Keys are
// fetched on first use, not here, so a Cognito outage at cold start does not
// crash-loop the function.
func New(region, userPoolID, clientID string) *Verifier {
	issuer := fmt.Sprintf("https://cognito-idp.%s.amazonaws.com/%s", region, userPoolID)
	return &Verifier{
		issuer:   issuer,
		audience: clientID,
		jwksURL:  issuer + "/.well-known/jwks.json",
		client:   &http.Client{Timeout: fetchTimeout},
		now:      time.Now,
	}
}

type cognitoClaims struct {
	jwt.RegisteredClaims
	TokenUse        string   `json:"token_use"`
	Email           string   `json:"email"`
	EmailVerified   flexBool `json:"email_verified"`
	CognitoUsername string   `json:"cognito:username"`
}

// flexBool is true only for JSON true or the string "true". Cognito writes a
// boolean for its own attributes, but an attribute mapped from a federated
// identity provider can arrive as a string.
type flexBool bool

func (b *flexBool) UnmarshalJSON(data []byte) error {
	s := string(data)
	*b = flexBool(s == "true" || s == `"true"`)
	return nil
}

// Verify accepts "Bearer <token>" (any case) or a bare token, as API Gateway
// does. Error messages name the check that failed, never the token or its
// claims.
func (v *Verifier) Verify(ctx context.Context, header string) (Claims, error) {
	raw := strings.TrimSpace(header)
	if scheme, rest, ok := strings.Cut(raw, " "); ok {
		if !strings.EqualFold(scheme, "Bearer") {
			return Claims{}, fmt.Errorf("%w: unsupported authorization scheme", ErrInvalid)
		}
		raw = strings.TrimSpace(rest)
	}
	if raw == "" {
		return Claims{}, fmt.Errorf("%w: no token", ErrInvalid)
	}

	var keyErr error
	parser := jwt.NewParser(
		// Checked before the key is looked up, so "none" and an HMAC token
		// signed with the public key never reach a verification step.
		jwt.WithValidMethods([]string{"RS256"}),
		jwt.WithIssuer(v.issuer),
		jwt.WithAudience(v.audience),
		jwt.WithExpirationRequired(),
		jwt.WithLeeway(leeway),
		jwt.WithTimeFunc(v.now),
	)
	claims := &cognitoClaims{}
	_, err := parser.ParseWithClaims(raw, claims, func(t *jwt.Token) (any, error) {
		kid, _ := t.Header["kid"].(string)
		if kid == "" {
			return nil, errors.New("no key id")
		}
		key, kerr := v.keyFor(ctx, kid)
		keyErr = kerr
		return key, kerr
	})
	// keyFor's own error already says exactly what happened - an outage or
	// an unknown key id - without ever touching attacker-controlled claim
	// text, so it is returned as-is instead of folded into the generic
	// mapping below, which would otherwise double its "idtoken: ..." prefix.
	if errors.Is(keyErr, ErrUnavailable) || errors.Is(keyErr, ErrInvalid) {
		return Claims{}, keyErr
	}
	if err != nil {
		// golang-jwt's own message can echo a claim's raw value (for
		// example a non-numeric "exp"), so only a fixed message naming the
		// failed check is used here, never err.Error() itself.
		return Claims{}, fmt.Errorf("%w: %s", ErrInvalid, jwtErrorMessage(err))
	}
	// WithAudience accepts a token whose aud merely includes the client.
	// Cognito's ID tokens carry exactly one.
	if len(claims.Audience) != 1 {
		return Claims{}, fmt.Errorf("%w: audience is not exactly the site client", ErrInvalid)
	}
	if claims.TokenUse != "id" {
		return Claims{}, fmt.Errorf("%w: not an ID token", ErrInvalid)
	}
	if claims.Subject == "" {
		return Claims{}, fmt.Errorf("%w: no subject", ErrInvalid)
	}
	return Claims{
		Sub:             claims.Subject,
		Email:           claims.Email,
		EmailVerified:   bool(claims.EmailVerified),
		CognitoUsername: claims.CognitoUsername,
	}, nil
}

// jwtErrorMessage maps a golang-jwt parse or validation error to a fixed
// message naming the check that failed. golang-jwt's own error text can
// include a claim's raw value (for example a non-numeric "exp"), so the
// message is never derived from err.Error() itself.
func jwtErrorMessage(err error) string {
	switch {
	case errors.Is(err, jwt.ErrTokenMalformed):
		return "malformed token"
	case errors.Is(err, jwt.ErrTokenSignatureInvalid):
		return "invalid signature or signing method"
	case errors.Is(err, jwt.ErrTokenUnverifiable):
		return "unverifiable token"
	case errors.Is(err, jwt.ErrTokenExpired):
		return "expired"
	case errors.Is(err, jwt.ErrTokenNotValidYet):
		return "not valid yet"
	case errors.Is(err, jwt.ErrTokenInvalidIssuer):
		return "wrong issuer"
	case errors.Is(err, jwt.ErrTokenInvalidAudience):
		return "wrong audience"
	case errors.Is(err, jwt.ErrTokenRequiredClaimMissing):
		return "required claim missing"
	default:
		return "token rejected"
	}
}

// keyFor returns the signing key with this ID. Until the first successful
// fetch, every call fetches. After that, an unknown ID fetches again at most
// once per refetchInterval, so a stream of junk key IDs costs Cognito one
// request per interval per execution environment.
func (v *Verifier) keyFor(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if v.keys == nil {
		if err := v.fetchLocked(ctx); err != nil {
			return nil, err
		}
	}
	if k, ok := v.keys[kid]; ok {
		return k, nil
	}
	if v.now().Sub(v.fetchedAt) >= refetchInterval {
		// The old keys stay in place either way; fetchLocked updates
		// fetchedAt itself, so a failed refresh still rate-limits the next
		// try.
		fetchErr := v.fetchLocked(ctx)
		if k, ok := v.keys[kid]; ok {
			return k, nil
		}
		if fetchErr != nil {
			// The refresh itself failed - an outage, not merely "this kid
			// is unknown" - so say that instead of blaming the caller.
			slog.Error("refreshing signing keys", "err", fetchErr)
			return nil, fetchErr
		}
	}
	return nil, fmt.Errorf("%w: unknown key id", ErrInvalid)
}

func (v *Verifier) fetchLocked(ctx context.Context) error {
	v.fetchedAt = v.now()
	ctx, cancel := context.WithTimeout(ctx, fetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, v.jwksURL, nil)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	resp, err := v.client.Do(req)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%w: status %d", ErrUnavailable, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, maxJWKSBytes+1))
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	if len(body) > maxJWKSBytes {
		return fmt.Errorf("%w: key set larger than %d bytes", ErrUnavailable, maxJWKSBytes)
	}
	keys, err := parseJWKS(body)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	v.keys = keys
	return nil
}

// parseJWKS keeps RSA signing keys of at least minKeyBits and ignores the
// rest, rather than failing on a key type it does not use.
func parseJWKS(body []byte) (map[string]*rsa.PublicKey, error) {
	var set struct {
		Keys []struct {
			Kty string `json:"kty"`
			Kid string `json:"kid"`
			Use string `json:"use"`
			N   string `json:"n"`
			E   string `json:"e"`
		} `json:"keys"`
	}
	if err := json.Unmarshal(body, &set); err != nil {
		return nil, err
	}
	keys := map[string]*rsa.PublicKey{}
	for _, k := range set.Keys {
		if k.Kty != "RSA" || k.Kid == "" || (k.Use != "" && k.Use != "sig") {
			continue
		}
		n, err := base64.RawURLEncoding.DecodeString(k.N)
		if err != nil {
			continue
		}
		e, err := base64.RawURLEncoding.DecodeString(k.E)
		if err != nil || len(e) == 0 || len(e) > 4 {
			continue
		}
		pub := &rsa.PublicKey{N: new(big.Int).SetBytes(n), E: int(new(big.Int).SetBytes(e).Int64())}
		if pub.N.BitLen() < minKeyBits || pub.E < 3 {
			continue
		}
		keys[k.Kid] = pub
	}
	if len(keys) == 0 {
		return nil, errors.New("no usable signing keys")
	}
	return keys, nil
}
