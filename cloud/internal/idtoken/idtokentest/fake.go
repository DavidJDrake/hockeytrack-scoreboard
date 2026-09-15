// Package idtokentest is a fake token verifier for handler tests. It accepts
// tokens nobody signed, so production code must never import it: the site's
// invoke-config tripwire fails the build if a non-test file does.
package idtokentest

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
	"strings"

	"hockeytrack-scoreboard/internal/idtoken"
)

const prefix = "Bearer fake:"

// Header returns an Authorization header that Fake accepts as carrying c.
func Header(c idtoken.Claims) string {
	q := url.Values{}
	q.Set("sub", c.Sub)
	q.Set("email", c.Email)
	q.Set("email_verified", strconv.FormatBool(c.EmailVerified))
	q.Set("cognito:username", c.CognitoUsername)
	return prefix + q.Encode()
}

// Fake accepts headers made by Header. With Unavailable set, every call
// fails as if Cognito's keys could not be fetched.
type Fake struct{ Unavailable bool }

func (f Fake) Verify(_ context.Context, header string) (idtoken.Claims, error) {
	if f.Unavailable {
		return idtoken.Claims{}, fmt.Errorf("%w: fake outage", idtoken.ErrUnavailable)
	}
	rest, ok := strings.CutPrefix(header, prefix)
	if !ok {
		return idtoken.Claims{}, fmt.Errorf("%w: not a fake token", idtoken.ErrInvalid)
	}
	q, err := url.ParseQuery(rest)
	if err != nil || q.Get("sub") == "" {
		return idtoken.Claims{}, fmt.Errorf("%w: fake token without a subject", idtoken.ErrInvalid)
	}
	return idtoken.Claims{
		Sub:             q.Get("sub"),
		Email:           q.Get("email"),
		EmailVerified:   q.Get("email_verified") == "true",
		CognitoUsername: q.Get("cognito:username"),
	}, nil
}
