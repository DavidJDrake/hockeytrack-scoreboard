// Command imagecheck compares, twice a day, three accounts of the scoreboard
// image strangers download: the image object in the mirror bucket, the
// checksum in the mirror's latest.json, and the checksum published with the
// GitHub Release, which is the source of truth. Two copies are a liability
// only if nobody notices them disagreeing; any disagreement goes to the
// security topic. See docs/superpowers/specs/2026-09-12-device-image-design.md, 9.6.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"strings"
	"time"
)

const alertSubject = "SCOREBOARD SECURITY: image mirror disagrees with its release, or the update panels will install"

const maxManifest = 64 << 10

var (
	versionRE = regexp.MustCompile(`^v[0-9]+\.[0-9]+\.[0-9]+$`)
	hexRE     = regexp.MustCompile(`^[0-9a-f]{64}$`)

	// ErrNotFound is an absent object in the mirror bucket.
	ErrNotFound = errors.New("object not found")
	// ErrNoRelease means the repository has published no release yet.
	ErrNoRelease = errors.New("no release published")
	// ErrNoChecksum means GitHub has no checksum asset for that release and
	// file. Unlike ErrNoRelease (nothing published at all), this happens
	// when latest.json names a version GitHub never released -- itself a
	// disagreement worth alerting on, not merely a failed check.
	ErrNoChecksum = errors.New("no release checksum found")
)

// Manifest is latest.json as the release workflow writes it.
type Manifest struct {
	Version  string `json:"version"`
	File     string `json:"file"`
	SHA256   string `json:"sha256"`
	Size     int64  `json:"size"`
	Released string `json:"released"`
	Release  string `json:"release"`
}

// Objects reads the mirror bucket.
type Objects interface {
	Get(ctx context.Context, key string) (io.ReadCloser, error)
}

// Releases reads the repository's public GitHub releases.
type Releases interface {
	LatestTag(ctx context.Context) (string, error)
	Checksum(ctx context.Context, tag, file string) (string, error)
}

// Notifier raises an alert.
type Notifier interface {
	Notify(ctx context.Context, subject, message string) error
}

// clip keeps attacker-writable text short before it goes into an alert.
func clip(s string) string {
	if len(s) > 80 {
		return s[:80] + "…"
	}
	return s
}

// Check returns every disagreement it finds, alongside an error if the
// check could not be completed in full. A non-nil error does not discard
// problems already found: whatever was collected before the failing step is
// still returned, so a forged manifest is never hidden behind an unrelated
// failure later in the comparison (Run publishes both).
func Check(ctx context.Context, objs Objects, rel Releases) ([]string, error) {
	latest, err := rel.LatestTag(ctx)
	noRelease := errors.Is(err, ErrNoRelease)
	if err != nil && !noRelease {
		return nil, fmt.Errorf("reading the latest GitHub release: %w", err)
	}

	body, err := objs.Get(ctx, "latest.json")
	if errors.Is(err, ErrNotFound) {
		if noRelease {
			return nil, nil
		}
		return []string{fmt.Sprintf("the mirror has no latest.json, but GitHub has release %s", clip(latest))}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading latest.json: %w", err)
	}
	raw, err := io.ReadAll(io.LimitReader(body, maxManifest+1))
	body.Close()
	if err != nil {
		return nil, fmt.Errorf("reading latest.json: %w", err)
	}

	var m Manifest
	if len(raw) > maxManifest || json.Unmarshal(raw, &m) != nil {
		return []string{"latest.json is not a valid manifest"}, nil
	}
	// The manifest is in a bucket someone may have written to, so nothing in
	// it is used as a key until it has the exact shape a release writes.
	if !versionRE.MatchString(m.Version) || m.File != "scoreboard-"+m.Version+".img.xz" || !hexRE.MatchString(m.SHA256) {
		return []string{fmt.Sprintf("latest.json names an unexpected version, file or checksum (version %q, file %q)", clip(m.Version), clip(m.File))}, nil
	}
	if noRelease {
		return []string{fmt.Sprintf("the mirror serves %s, but GitHub has no release at all", m.Version)}, nil
	}

	var problems []string
	if latest != m.Version {
		problems = append(problems, fmt.Sprintf("the mirror serves %s but the latest GitHub release is %s", m.Version, clip(latest)))
	}

	// A 404 here means latest.json names a version (or file) GitHub never
	// released -- a disagreement in its own right, not a failed check, so
	// it is reported as a problem instead of discarding what was already
	// found above and surfacing only as an error.
	released, err := rel.Checksum(ctx, m.Version, m.File)
	if errors.Is(err, ErrNoChecksum) {
		return append(problems, fmt.Sprintf("GitHub has no release checksum for %s", m.Version)), nil
	}
	if err != nil {
		return problems, fmt.Errorf("reading the GitHub release checksum for %s: %w", m.Version, err)
	}
	if released != m.SHA256 {
		problems = append(problems, fmt.Sprintf("latest.json's checksum for %s differs from the GitHub release's", m.Version))
	}

	obj, err := objs.Get(ctx, "images/"+m.Version+"/"+m.File)
	if errors.Is(err, ErrNotFound) {
		return append(problems, fmt.Sprintf("the mirror has no image object for %s", m.Version)), nil
	}
	if err != nil {
		return problems, fmt.Errorf("reading the image object: %w", err)
	}
	h := sha256.New()
	n, err := io.Copy(h, obj)
	obj.Close()
	if err != nil {
		return problems, fmt.Errorf("hashing the image object: %w", err)
	}
	if hex.EncodeToString(h.Sum(nil)) != released {
		problems = append(problems, fmt.Sprintf("the mirrored image for %s does not hash to the GitHub release's checksum", m.Version))
	}
	if n != m.Size {
		problems = append(problems, fmt.Sprintf("the mirrored image for %s is %d bytes but latest.json says %d bytes", m.Version, n, m.Size))
	}
	return problems, nil
}

// latestVersion is the version latest.json names when it has a release's
// shape, or "" when Check has already reported it unusable.
func latestVersion(ctx context.Context, objs Objects) string {
	raw, err := getAll(ctx, objs, "latest.json", maxManifest)
	if err != nil {
		return ""
	}
	var m Manifest
	if json.Unmarshal(raw, &m) != nil || !versionRE.MatchString(m.Version) {
		return ""
	}
	return m.Version
}

// Signing is what the signed-manifest check needs (signed.go). It is nil
// only in tests of the older checks; the Lambda always has one.
type Signing struct {
	Assets Assets
	Key    SigningKey
	Now    func() time.Time
}

// Run checks once and raises one alert naming every problem found. A
// problem is published even when Check also returns an error, because a
// disagreement found before a later failure is real: the caller (the
// scheduled Lambda) should still see the error too, so its own errors alarm
// fires and someone looks at why the check could not finish. The signed
// manifest is checked after the image, for the version latest.json names,
// only when latest.json itself was readable enough to name one.
func Run(ctx context.Context, objs Objects, rel Releases, n Notifier, signing *Signing) error {
	problems, err := Check(ctx, objs, rel)
	if signing != nil && err == nil {
		if version := latestVersion(ctx, objs); version != "" {
			more, signedErr := CheckSigned(ctx, objs, signing.Assets, signing.Key, signing.Now(), version)
			problems = append(problems, more...)
			err = signedErr
		}
	}
	if len(problems) == 0 {
		if err == nil {
			slog.Info("image mirror agrees with its release")
		}
		return err
	}
	slog.Warn("image mirror disagrees with its release", "problems", len(problems))
	msg := "The scoreboard image mirror at images.scoreboard.davidjdrake.com disagrees with its GitHub release:\n\n- " +
		strings.Join(problems, "\n- ") +
		"\n\nIf this was not a release in progress, assume the image strangers download, or the update panels will install, may have been replaced. " +
		"See HockeyTrack's docs/threat-model.md, section 7."
	if notifyErr := n.Notify(ctx, alertSubject, msg); notifyErr != nil {
		return errors.Join(err, notifyErr)
	}
	return err
}
