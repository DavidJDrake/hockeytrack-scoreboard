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

// maxCodeAttempts bounds retrying a colliding code. A collision is a
// one-in-a-trillion event the caller can do nothing about; retrying it is
// free, so the 500 this falls through to is reserved for something actually
// wrong rather than bad luck on the first draw.
const maxCodeAttempts = 5

type Handler struct {
	Enrollments enroll.Store
	Devices     devices.Store
	Issuer      enroll.Issuer
	IoTEndpoint string
	TTL         time.Duration
	CodeTTL     time.Duration
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
	token, err := enroll.Token()
	if err != nil {
		return fail(http.StatusInternalServerError, "enrollment failed")
	}
	thingName, err := enroll.ThingName()
	if err != nil {
		return fail(http.StatusInternalServerError, "enrollment failed")
	}
	pending := enroll.Pending{
		TokenHash: enroll.HashSecret(token),
		CSR:       in.CSR,
		ThingName: thingName,
		ExpiresAt: time.Now().Add(h.TTL).Unix(),
	}
	// An owner hint binds this enrollment to one person, so the code on the
	// screen is useless to anybody else. Absent is fine: the panel then
	// behaves as an open enrollment with short rotating codes.
	if hint := enroll.NormalizeOwner(in.Owner); hint != "" {
		pending.OwnerHintHash = enroll.HashSecret(hint)
	}
	// A duplicate code is not the caller's problem: try again with another
	// rather than surfacing it. The same retry rotate uses below.
	var code string
	var lastErr error
	for attempt := 0; attempt < maxCodeAttempts; attempt++ {
		code, err = enroll.Code()
		if err != nil {
			return fail(http.StatusInternalServerError, "enrollment failed")
		}
		pending.CodeHash = enroll.HashSecret(code)
		pending.CodeExpiresAt = time.Now().Add(h.CodeTTL).Unix()
		if err := h.Enrollments.Create(ctx, pending); err == nil {
			lastErr = nil
			break
		} else if errors.Is(err, enroll.ErrCodeTaken) {
			lastErr = err
			continue
		} else {
			slog.Error("creating enrollment", "err", err)
			return fail(http.StatusInternalServerError, "enrollment failed")
		}
	}
	if lastErr != nil {
		slog.Error("creating enrollment: exhausted code retries", "err", lastErr)
		return fail(http.StatusInternalServerError, "enrollment failed")
	}
	// The raw code and token are returned once, here, and never stored.
	return respond(http.StatusCreated, map[string]any{
		"code":        code,
		"display":     enroll.FormatCode(code),
		"token":       token,
		"pollSeconds": 5,
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
	for attempt := 0; attempt < maxCodeAttempts; attempt++ {
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
	// Register and Claim run before Issue. If either fails, nothing has been
	// minted in AWS -- the enrollment stays claiming and the owner sees
	// nothing. Ordering it the other way around would let Issue succeed and
	// then leave an active certificate, thing and attached policy with no
	// devices row at all: no way for the owner to see it, unbind it, or
	// trigger any cleanup. This way, once a devices row exists, a later
	// failure (Issue or SetCertificate) leaves an owned row the owner can see
	// and unbind -- which is the recovery path the spec describes.
	if err := h.Devices.Register(ctx, p.ThingName); err != nil {
		slog.Error("registering device", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	if err := h.Devices.Claim(ctx, p.ThingName, sub); err != nil {
		slog.Error("claiming device", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	certPEM, err := h.Issuer.Issue(ctx, p.CSR, p.ThingName)
	if err != nil {
		slog.Error("issuing certificate", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	if err := h.Enrollments.SetCertificate(ctx, p.TokenHash, certPEM); err != nil {
		slog.Error("storing certificate", "thing", p.ThingName, "err", err)
		return fail(http.StatusInternalServerError, "claim failed")
	}
	return respond(http.StatusOK, map[string]string{"thingName": p.ThingName})
}
