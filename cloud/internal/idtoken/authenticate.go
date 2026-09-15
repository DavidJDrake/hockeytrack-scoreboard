package idtoken

import (
	"context"
	"errors"
	"log/slog"
	"strings"

	"github.com/aws/aws-lambda-go/events"
)

// MismatchMessage is logged when API Gateway's authorizer accepted a request
// whose token Verify refuses. This is not proof of a hand-built event: it
// also happens on two real paths. An HTTP API JWT authorizer validates
// client_id, not aud, on a token that carries no aud at all, so a Cognito
// access token issued to the scoreboard-site client passes the authorizer
// (no route here sets scopes) and Verify then refuses it for missing aud /
// not being an ID token. A Cognito signing-key rotation can do the same if
// the new kid appears within the verifier's 5-minute refetch window: the
// authorizer's own key cache picks it up first. Both leave the request
// refused with 401, nothing exposed. Telling them apart from a genuine
// direct invoke is HockeyTrack's job: a mismatch page with no section 12
// page at the same time came through API Gateway (check the access log for
// the route and sub); a mismatch page together with a section 12 page is a
// direct invoke. The scoreboard-token-mismatch metric filters
// (terraform/admin.tf) match this exact text.
const MismatchMessage = "token rejected after authorizer accepted"

// Authenticate verifies the request's Authorization header and returns the
// caller. It never reads identity from the authorizer block; it only notes
// whether one was present, to log a mismatch.
func Authenticate(ctx context.Context, t Tokens, req events.APIGatewayV2HTTPRequest) (Claims, error) {
	header := ""
	for name, value := range req.Headers {
		if strings.EqualFold(name, "authorization") {
			header = value
			break
		}
	}
	claims, err := t.Verify(ctx, header)
	if err == nil {
		return claims, nil
	}
	route := req.RequestContext.RouteKey
	if route == "" {
		route = req.RouteKey
	}
	if errors.Is(err, ErrUnavailable) {
		slog.Error("token verification unavailable", "route", route, "err", err)
		return Claims{}, err
	}
	if req.RequestContext.Authorizer != nil && req.RequestContext.Authorizer.JWT != nil {
		slog.Warn(MismatchMessage, "route", route, "check", err.Error())
	}
	return Claims{}, err
}
