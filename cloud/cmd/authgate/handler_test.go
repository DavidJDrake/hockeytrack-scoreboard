package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"
)

const invited = "owner@example.com"

// gate is a Handler wired to a fake parameter, a movable clock and a log
// buffer the test can read.
type gate struct {
	*Handler
	src   *source
	clock *clock
	logs  *bytes.Buffer
}

func newGate(list string) *gate {
	src := &source{value: list}
	c := newClock()
	logs := &bytes.Buffer{}
	return &gate{
		Handler: &Handler{
			Allowlist: newAllowlist(src, c),
			Log:       slog.New(slog.NewTextHandler(logs, nil)),
		},
		src:   src,
		clock: c,
		logs:  logs,
	}
}

// event builds a trigger event in the shape Cognito sends one.
func event(t *testing.T, triggerSource string, attrs map[string]string) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(map[string]any{
		"version":       "1",
		"region":        "us-east-1",
		"userPoolId":    "us-east-1_example",
		"userName":      "Google_117000000000000000000",
		"callerContext": map[string]string{"awsSdkVersion": "aws-sdk-unknown-unknown", "clientId": "client123"},
		"triggerSource": triggerSource,
		"request":       map[string]any{"userAttributes": attrs},
		"response":      map[string]any{},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func verified(email string) map[string]string {
	return map[string]string{"email": email, "email_verified": "true"}
}

func mustRefuse(t *testing.T, out json.RawMessage, err error) {
	t.Helper()
	// Identity, not errors.Is: a wrapped error would carry extra text to the
	// browser, and the point is that every refusal reads the same.
	if err != errRefused {
		t.Fatalf("err = %v, want errRefused", err)
	}
	if out != nil {
		t.Fatalf("a refusal returned an event: %s", out)
	}
}

func TestPreSignUpAdmitsAnInvitedVerifiedGoogleAccount(t *testing.T) {
	g := newGate(invited)
	out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified(invited)))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
	var got events.CognitoEventUserPoolsPreSignup
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatal(err)
	}
	if !got.Response.AutoConfirmUser || !got.Response.AutoVerifyEmail {
		t.Fatalf("response = %+v, want the account confirmed and its email verified", got.Response)
	}
	if got.TriggerSource != "PreSignUp_ExternalProvider" || got.UserName != "Google_117000000000000000000" {
		t.Fatalf("header not returned intact: %+v", got.CognitoEventUserPoolsHeader)
	}
}

func TestPreSignUpRefusesEveryOtherSourceEvenForAnInvitedAddress(t *testing.T) {
	// ExternalProviderX proves the check is an exact match, not a prefix.
	for _, src := range []string{"PreSignUp_SignUp", "PreSignUp_AdminCreateUser", "PreSignUp_ExternalProviderX"} {
		t.Run(src, func(t *testing.T) {
			g := newGate(invited)
			out, err := g.Handle(context.Background(), event(t, src, verified(invited)))
			mustRefuse(t, out, err)
		})
	}
}

func TestPreSignUpRefusesAnAddressGoogleHasNotVerified(t *testing.T) {
	cases := map[string]map[string]string{
		"false":   {"email": invited, "email_verified": "false"},
		"empty":   {"email": invited, "email_verified": ""},
		"missing": {"email": invited},
	}
	for name, attrs := range cases {
		t.Run(name, func(t *testing.T) {
			g := newGate(invited)
			out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", attrs))
			mustRefuse(t, out, err)
		})
	}
}

func TestPreSignUpRefusesAnUninvitedAddress(t *testing.T) {
	g := newGate(invited)
	out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified("stranger@example.com")))
	mustRefuse(t, out, err)
}

func TestPreSignUpIgnoresCaseAndSurroundingWhitespace(t *testing.T) {
	g := newGate(" Owner@Example.com ")
	_, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified("OWNER@example.COM ")))
	if err != nil {
		t.Fatalf("refused: %v", err)
	}
}

func TestAnUnreadableInviteListRefusesAtBothTriggers(t *testing.T) {
	for _, src := range []string{"PreSignUp_ExternalProvider", "TokenGeneration_HostedAuth"} {
		t.Run(src, func(t *testing.T) {
			g := newGate(invited)
			g.src.err = errors.New("ssm unavailable")
			out, err := g.Handle(context.Background(), event(t, src, verified(invited)))
			mustRefuse(t, out, err)
			if !strings.Contains(g.logs.String(), `reason="invite list unavailable"`) {
				t.Fatalf("log does not name the cause:\n%s", g.logs)
			}
		})
	}
}

func TestTokenGenerationPassesAnInvitedAccountThroughUnchanged(t *testing.T) {
	for _, src := range []string{"TokenGeneration_HostedAuth", "TokenGeneration_RefreshTokens", "TokenGeneration_Authentication"} {
		t.Run(src, func(t *testing.T) {
			g := newGate(invited)
			in := event(t, src, verified(invited))
			out, err := g.Handle(context.Background(), in)
			if err != nil {
				t.Fatalf("refused: %v", err)
			}
			if !bytes.Equal(out, in) {
				t.Fatalf("event changed:\n in: %s\nout: %s", in, out)
			}
		})
	}
}

func TestTokenGenerationRefusesAnAddressTakenOffTheList(t *testing.T) {
	g := newGate(invited)
	if _, err := g.Handle(context.Background(), event(t, "TokenGeneration_HostedAuth", verified(invited))); err != nil {
		t.Fatalf("refused while still invited: %v", err)
	}
	g.src.value = "someone.else@example.com"
	g.clock.advance(time.Minute)
	out, err := g.Handle(context.Background(), event(t, "TokenGeneration_RefreshTokens", verified(invited)))
	mustRefuse(t, out, err)
}

func TestUnexpectedEventsAreRefused(t *testing.T) {
	cases := map[string]json.RawMessage{
		"another trigger":   event(t, "CustomMessage_SignUp", verified(invited)),
		"no trigger source": event(t, "", verified(invited)),
		"not JSON":          json.RawMessage(`{`),
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			g := newGate(invited)
			out, err := g.Handle(context.Background(), in)
			mustRefuse(t, out, err)
		})
	}
}

func TestRefusalsLogTheDomainButNotWhoItWas(t *testing.T) {
	g := newGate(invited)
	out, err := g.Handle(context.Background(), event(t, "PreSignUp_ExternalProvider", verified("Stranger.Name@Example.com")))
	mustRefuse(t, out, err)

	logged := g.logs.String()
	for _, want := range []string{"sign-in refused", "trigger=PreSignUp_ExternalProvider", `reason="not invited"`, "domain=example.com"} {
		if !strings.Contains(logged, want) {
			t.Errorf("log lacks %q:\n%s", want, logged)
		}
	}
	if strings.Contains(strings.ToLower(logged), "stranger.name") {
		t.Errorf("log contains the address's local part:\n%s", logged)
	}
}
