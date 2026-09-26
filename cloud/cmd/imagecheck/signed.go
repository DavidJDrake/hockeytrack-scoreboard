package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"
)

// Since 2026-09-25 a release also carries a signed per-version manifest
// (docs/superpowers/specs/2026-09-25-ota-update-design.md, 6.2 and 6.5): the
// thing a panel trusts before it installs an update unattended. The monitor
// therefore also verifies the mirrored manifest's signature against the KMS
// key's public half, read from KMS at run time rather than from the
// repository, so a swapped key in the repository does not fool it; checks the
// manifest's version equals latest.json's; hashes both payload objects
// against the manifest's sha256 and size; compares the manifest to the
// GitHub Release's copy; and pages when it is within 14 days of expiring,
// because after that lagging panels stop accepting the last release until a
// new one is cut.

// expiryWarning is how far ahead of a manifest's expiry the monitor pages.
const expiryWarning = 14 * 24 * time.Hour

// maxPayload bounds a payload object read; a root payload is under 1 GB.
const maxPayload = 2 << 30

// ErrNoAsset means the GitHub release has no asset by that name.
var ErrNoAsset = errors.New("no release asset found")

// SigningKey reads the release-signing key's public half from KMS as a DER
// SubjectPublicKeyInfo.
type SigningKey interface {
	PublicKey(ctx context.Context) ([]byte, error)
}

// Assets reads a small file published with a GitHub release.
type Assets interface {
	Asset(ctx context.Context, tag, file string) ([]byte, error)
}

// ReleaseManifest is the signed per-version manifest, as far as the monitor
// reads it. Nothing in it is trusted before the signature verifies.
type ReleaseManifest struct {
	Version  string `json:"version"`
	Channel  string `json:"channel"`
	Released string `json:"released"`
	Expires  string `json:"expires"`
	Layout   int    `json:"layout"`
	Payloads map[string]struct {
		File   string `json:"file"`
		Size   int64  `json:"size"`
		SHA256 string `json:"sha256"`
	} `json:"payloads"`
	KeyID string `json:"keyId"`
}

// Verify checks a base64 DER ECDSA-P256/SHA-256 signature over the exact
// manifest bytes against a DER public key. It is the same rule the panel
// applies; nothing is parsed until it holds.
func Verify(manifest, signatureB64, publicKeyDER []byte) error {
	pub, err := x509.ParsePKIXPublicKey(publicKeyDER)
	if err != nil {
		return fmt.Errorf("the signing key is not a DER public key: %w", err)
	}
	ec, ok := pub.(*ecdsa.PublicKey)
	if !ok {
		return errors.New("the signing key is not an ECDSA key")
	}
	sig, err := base64.StdEncoding.DecodeString(string(bytes.TrimSpace(signatureB64)))
	if err != nil {
		return errors.New("the signature is not base64")
	}
	digest := sha256.Sum256(manifest)
	if !ecdsa.VerifyASN1(ec, digest[:], sig) {
		return errors.New("the signature does not verify")
	}
	return nil
}

// CheckSigned verifies the mirrored manifest for version and returns every
// disagreement, or an error when the check could not be completed. A release
// made before manifests existed (neither the mirror nor GitHub has one) is
// not a disagreement; a manifest on one side but not the other is.
func CheckSigned(ctx context.Context, objs Objects, assets Assets, key SigningKey, now time.Time, version string) ([]string, error) {
	name := "scoreboard-" + version + ".manifest.json"
	raw, mirrorErr := getAll(ctx, objs, "images/"+version+"/"+name, maxManifest)
	published, ghErr := assets.Asset(ctx, version, name)
	if errors.Is(mirrorErr, ErrNotFound) && errors.Is(ghErr, ErrNoAsset) {
		return nil, nil // a release from before the update path existed
	}
	if errors.Is(mirrorErr, ErrNotFound) {
		return []string{fmt.Sprintf("the mirror has no signed manifest for %s, but the GitHub release has one", version)}, nil
	}
	if mirrorErr != nil {
		return nil, fmt.Errorf("reading the mirrored manifest: %w", mirrorErr)
	}
	var problems []string
	if errors.Is(ghErr, ErrNoAsset) {
		problems = append(problems, fmt.Sprintf("the mirror has a signed manifest for %s, but the GitHub release has none", version))
	} else if ghErr != nil {
		return nil, fmt.Errorf("reading the GitHub release's manifest: %w", ghErr)
	} else if !bytes.Equal(raw, published) {
		problems = append(problems, fmt.Sprintf("the mirrored manifest for %s differs from the GitHub release's", version))
	}

	sig, err := getAll(ctx, objs, "images/"+version+"/scoreboard-"+version+".manifest.sig", 4096)
	if errors.Is(err, ErrNotFound) {
		return append(problems, fmt.Sprintf("the mirror has no manifest signature for %s", version)), nil
	}
	if err != nil {
		return problems, fmt.Errorf("reading the manifest signature: %w", err)
	}
	pub, err := key.PublicKey(ctx)
	if err != nil {
		return problems, fmt.Errorf("reading the signing key from KMS: %w", err)
	}
	if err := Verify(raw, sig, pub); err != nil {
		// Nothing below is trusted, so nothing below is checked: a manifest
		// that does not verify is the finding.
		return append(problems, fmt.Sprintf("the mirrored manifest for %s does not verify against the KMS signing key: %v", version, err)), nil
	}

	var m ReleaseManifest
	if json.Unmarshal(raw, &m) != nil {
		return append(problems, fmt.Sprintf("the signed manifest for %s is not valid JSON", version)), nil
	}
	if m.Version != version {
		problems = append(problems, fmt.Sprintf("the signed manifest names %s but latest.json names %s", clip(m.Version), version))
	}
	if m.Channel != "stable" {
		problems = append(problems, fmt.Sprintf("latest.json points at a manifest for the %s channel", clip(m.Channel)))
	}
	expires, err := time.Parse(time.RFC3339, m.Expires)
	switch {
	case err != nil:
		problems = append(problems, fmt.Sprintf("the signed manifest for %s has an unreadable expiry", version))
	case !expires.After(now):
		problems = append(problems, fmt.Sprintf("the signed manifest for %s expired on %s; no panel will accept it", version, expires.Format("2006-01-02")))
	case expires.Sub(now) < expiryWarning:
		// An operations reminder, not a finding about the mirror. It goes out
		// under the same security subject as everything else this monitor
		// says, so the sentence has to say on its own that nothing was
		// replaced, or a reader who sees the subject twice a month for a
		// fortnight learns to ignore it.
		problems = append(problems, fmt.Sprintf("not a compromise, a deadline: the signed manifest for %s expires on %s, within 14 days, and no newer release has been published; cut a release before then or lagging panels stop updating", version, expires.Format("2006-01-02")))
	}
	for _, kind := range []string{"boot", "root"} {
		p, ok := m.Payloads[kind]
		if !ok || p.File != "scoreboard-"+version+"."+kind+".img.xz" || !hexRE.MatchString(p.SHA256) {
			problems = append(problems, fmt.Sprintf("the signed manifest for %s names an unexpected %s payload", version, kind))
			continue
		}
		obj, err := objs.Get(ctx, "images/"+version+"/"+p.File)
		if errors.Is(err, ErrNotFound) {
			problems = append(problems, fmt.Sprintf("the mirror has no %s payload for %s", kind, version))
			continue
		}
		if err != nil {
			return problems, fmt.Errorf("reading the %s payload: %w", kind, err)
		}
		h := sha256.New()
		n, err := io.Copy(h, io.LimitReader(obj, maxPayload))
		obj.Close()
		if err != nil {
			return problems, fmt.Errorf("hashing the %s payload: %w", kind, err)
		}
		if hex.EncodeToString(h.Sum(nil)) != p.SHA256 {
			problems = append(problems, fmt.Sprintf("the mirrored %s payload for %s does not hash to what the signed manifest says; a panel would refuse it, and something wrote the mirror", kind, version))
		}
		if n != p.Size {
			problems = append(problems, fmt.Sprintf("the mirrored %s payload for %s is %d bytes but the manifest says %d", kind, version, n, p.Size))
		}
	}
	return problems, nil
}

func getAll(ctx context.Context, objs Objects, key string, limit int64) ([]byte, error) {
	body, err := objs.Get(ctx, key)
	if err != nil {
		return nil, err
	}
	defer body.Close()
	raw, err := io.ReadAll(io.LimitReader(body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > limit {
		return nil, fmt.Errorf("%s is larger than %d bytes", key, limit)
	}
	return raw, nil
}
