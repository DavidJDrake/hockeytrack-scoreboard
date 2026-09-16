package idtoken

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	"github.com/aws/aws-lambda-go/events"
)

type tokensFunc func(context.Context, string) (Claims, error)

func (f tokensFunc) Verify(ctx context.Context, h string) (Claims, error) { return f(ctx, h) }

func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

func withAuthorizer(r events.APIGatewayV2HTTPRequest) events.APIGatewayV2HTTPRequest {
	r.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
		JWT: &events.APIGatewayV2HTTPRequestContextAuthorizerJWTDescription{
			Claims: map[string]string{"sub": "forged-owner-sub"},
		},
	}
	return r
}

func TestAuthenticateReadsTheAuthorizationHeaderInAnyCase(t *testing.T) {
	var seen string
	tokens := tokensFunc(func(_ context.Context, h string) (Claims, error) {
		seen = h
		return Claims{Sub: "sub-1"}, nil
	})
	req := events.APIGatewayV2HTTPRequest{Headers: map[string]string{"Authorization": "Bearer abc"}}
	got, err := Authenticate(context.Background(), tokens, req)
	if err != nil || got.Sub != "sub-1" || seen != "Bearer abc" {
		t.Errorf("got %+v, %v; verifier saw %q", got, err, seen)
	}
}

func TestAuthenticateLogsAMismatchWhenTheAuthorizerHadAcceptedTheCall(t *testing.T) {
	buf := captureLog(t)
	tokens := tokensFunc(func(context.Context, string) (Claims, error) {
		return Claims{}, fmt.Errorf("%w: token has invalid issuer", ErrInvalid)
	})
	req := withAuthorizer(events.APIGatewayV2HTTPRequest{
		RouteKey: "GET /api/devices",
		Headers:  map[string]string{"authorization": "Bearer secret-token-value"},
	})
	if _, err := Authenticate(context.Background(), tokens, req); !errors.Is(err, ErrInvalid) {
		t.Fatalf("err = %v, want ErrInvalid", err)
	}
	out := buf.String()
	if !strings.Contains(out, MismatchMessage) || !strings.Contains(out, "GET /api/devices") {
		t.Errorf("log lacks the mismatch line or route: %s", out)
	}
	for _, secret := range []string{"secret-token-value", "forged-owner-sub"} {
		if strings.Contains(out, secret) {
			t.Errorf("log contains %q: %s", secret, out)
		}
	}
}

func TestAuthenticateDoesNotCallAMissingTokenAMismatchWithoutAnAuthorizer(t *testing.T) {
	buf := captureLog(t)
	tokens := tokensFunc(func(context.Context, string) (Claims, error) {
		return Claims{}, fmt.Errorf("%w: no token", ErrInvalid)
	})
	if _, err := Authenticate(context.Background(), tokens, events.APIGatewayV2HTTPRequest{}); !errors.Is(err, ErrInvalid) {
		t.Fatalf("err = %v, want ErrInvalid", err)
	}
	if strings.Contains(buf.String(), MismatchMessage) {
		t.Errorf("logged a mismatch with no authorizer block: %s", buf.String())
	}
}

func TestAuthenticatePassesUnavailableThroughWithoutAMismatch(t *testing.T) {
	buf := captureLog(t)
	tokens := tokensFunc(func(context.Context, string) (Claims, error) {
		return Claims{}, fmt.Errorf("%w: status 503", ErrUnavailable)
	})
	req := withAuthorizer(events.APIGatewayV2HTTPRequest{RouteKey: "GET /api/devices"})
	if _, err := Authenticate(context.Background(), tokens, req); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v, want ErrUnavailable", err)
	}
	if strings.Contains(buf.String(), MismatchMessage) {
		t.Errorf("an outage was logged as a mismatch: %s", buf.String())
	}
}
