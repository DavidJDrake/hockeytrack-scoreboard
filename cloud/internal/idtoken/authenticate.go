package idtoken

import (
	"context"
	"errors"
	"log/slog"
	"strings"

	"github.com/aws/aws-lambda-go/events"
)

// MismatchMessage is logged when API Gateway's authorizer accepted a request
// whose token Verify refuses. On the real path both check the same token
// against the same issuer and audience, so this is what a hand-built event
// carrying authorizer claims looks like. The scoreboard-token-mismatch metric
// filters (terraform/admin.tf) match this exact text.
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
