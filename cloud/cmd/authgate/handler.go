// Command authgate decides who may sign in to the admin site.
//
// Cognito calls it at both ends of an account's life. As the pre sign-up
// trigger, it runs when a Google account signs in for the first time and
// Cognito is about to create a profile for it. As the pre token generation
// trigger, it runs every time tokens are issued. The pool's own invite-only
// setting governs the SignUp operation and nothing else, so without this
// function anyone with a Google account could make an account here. The
// design is docs/superpowers/specs/2026-09-13-google-sign-in-design.md.
//
// Every doubt resolves to no. Returning an error is how a trigger makes
// Cognito refuse, so a broken dependency refuses too.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"

	"github.com/aws/aws-lambda-go/events"

	"hockeytrack-scoreboard/internal/enroll"
)

// errRefused is the one answer every refusal gives. Cognito passes the
// message through to the browser, so it must not say which check failed:
// "not verified" against "not invited" would tell a stranger whether an
// address is on the list.
var errRefused = errors.New("this account is not invited")

type Handler struct {
	Allowlist *Allowlist
	Log       *slog.Logger
}

func (h *Handler) Handle(ctx context.Context, event json.RawMessage) (json.RawMessage, error) {
	var header events.CognitoEventUserPoolsHeader
	if err := json.Unmarshal(event, &header); err != nil {
		return h.refuse("", "malformed event", "")
	}
	switch {
	case strings.HasPrefix(header.TriggerSource, "PreSignUp_"):
		return h.preSignUp(ctx, event)
	case strings.HasPrefix(header.TriggerSource, "TokenGeneration_"):
		return h.tokenGeneration(ctx, event)
	}
	return h.refuse(header.TriggerSource, "unexpected trigger", "")
}

// preSignUp admits a new account only for a Google sign-in, with an address
// Google has verified, that is on the invite list.
func (h *Handler) preSignUp(ctx context.Context, event json.RawMessage) (json.RawMessage, error) {
	var ev events.CognitoEventUserPoolsPreSignup
	if err := json.Unmarshal(event, &ev); err != nil {
		return h.refuse("", "malformed event", "")
	}
	source := ev.TriggerSource
	email := ev.Request.UserAttributes["email"]

	// PreSignUp_SignUp and PreSignUp_AdminCreateUser are refused for every
	// address, invited or not. Otherwise anyone who knew an invited address
	// could make a password account for it, one Google never vouched for.
	if source != "PreSignUp_ExternalProvider" {
		return h.refuse(source, "not a Google sign-in", email)
	}
	// Panel ownership is decided from the verified email (ownerMatches in
	// cmd/enroll), so this site must not vouch for an address its identity
	// provider has not.
	if ev.Request.UserAttributes["email_verified"] != "true" {
		return h.refuse(source, "email not verified by Google", email)
	}
	if out, err := h.requireInvited(ctx, source, email); err != nil {
		return out, err
	}

	ev.Response.AutoConfirmUser = true
	ev.Response.AutoVerifyEmail = true
	return json.Marshal(ev)
}

// tokenGenerationEvent is the part of a token generation event the gate reads,
// and deliberately nothing more. aws-lambda-go's full type also decodes
// request.groupConfiguration.preferredRole as a string, which is what AWS's
// syntax reference says it is -- yet the example test events on the same
// documentation page show it as an array. A field this function never looks at
// must not be able to turn every sign-in into a refusal.
type tokenGenerationEvent struct {
	TriggerSource string `json:"triggerSource"`
	Request       struct {
		UserAttributes map[string]string `json:"userAttributes"`
	} `json:"request"`
}

// tokenGeneration refuses tokens to an account whose address has since been
// taken off the invite list. Checking only at sign-up would leave a removed
// person's account working indefinitely. It changes no claim: the event goes
// back exactly as it came.
func (h *Handler) tokenGeneration(ctx context.Context, event json.RawMessage) (json.RawMessage, error) {
	var ev tokenGenerationEvent
	if err := json.Unmarshal(event, &ev); err != nil {
		return h.refuse("", "malformed event", "")
	}
	if out, err := h.requireInvited(ctx, ev.TriggerSource, ev.Request.UserAttributes["email"]); err != nil {
		return out, err
	}
	return event, nil
}

// requireInvited returns a nil error only when email is on the invite list.
func (h *Handler) requireInvited(ctx context.Context, source, email string) (json.RawMessage, error) {
	invited, err := h.Allowlist.Contains(ctx, email)
	if err != nil {
		return h.refuse(source, "invite list unavailable", email, "err", err)
	}
	if !invited {
		return h.refuse(source, "not invited", email)
	}
	return nil, nil
}

// refuse logs one line -- the metric filter in terraform/signin.tf counts
// exactly this message -- and returns the refusal.
func (h *Handler) refuse(source, reason, email string, attrs ...any) (json.RawMessage, error) {
	args := append([]any{"trigger", source, "reason", reason, "domain", domainOf(email)}, attrs...)
	h.Log.Warn("sign-in refused", args...)
	return nil, errRefused
}

// domainOf keeps the part of an address that says roughly where a refusal
// came from -- gmail.com, or a company -- and drops the part that says who.
// A stranger's address is personal data this project has no reason to keep.
func domainOf(email string) string {
	at := strings.LastIndex(email, "@")
	if at < 0 {
		return ""
	}
	return enroll.NormalizeOwner(email[at+1:])
}
