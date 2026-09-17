// Command imagecheck compares, once a day, three accounts of the scoreboard
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
)

const alertSubject = "SCOREBOARD SECURITY: image mirror disagrees with its release"

const maxManifest = 64 << 10

var (
	versionRE = regexp.MustCompile(`^v[0-9]+\.[0-9]+\.[0-9]+$`)
	hexRE     = regexp.MustCompile(`^[0-9a-f]{64}$`)

	// ErrNotFound is an absent object in the mirror bucket.
	ErrNotFound = errors.New("object not found")
	// ErrNoRelease means the repository has published no release yet.
	ErrNoRelease = errors.New("no release published")
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

// Check returns every disagreement it finds. An error means the check could
// not be completed, which is not the same as the mirror being wrong.
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
	released, err := rel.Checksum(ctx, m.Version, m.File)
	if err != nil {
		return nil, fmt.Errorf("reading the GitHub release checksum for %s: %w", m.Version, err)
	}
	if released != m.SHA256 {
		problems = append(problems, fmt.Sprintf("latest.json's checksum for %s differs from the GitHub release's", m.Version))
	}

	obj, err := objs.Get(ctx, "images/"+m.Version+"/"+m.File)
	if errors.Is(err, ErrNotFound) {
		return append(problems, fmt.Sprintf("the mirror has no image object for %s", m.Version)), nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading the image object: %w", err)
	}
	h := sha256.New()
	_, err = io.Copy(h, obj)
	obj.Close()
	if err != nil {
		return nil, fmt.Errorf("hashing the image object: %w", err)
	}
	if hex.EncodeToString(h.Sum(nil)) != released {
		problems = append(problems, fmt.Sprintf("the mirrored image for %s does not hash to the GitHub release's checksum", m.Version))
	}
	return problems, nil
}

// Run checks once and raises one alert naming every problem found.
func Run(ctx context.Context, objs Objects, rel Releases, n Notifier) error {
	problems, err := Check(ctx, objs, rel)
	if err != nil {
		return err
	}
	if len(problems) == 0 {
		slog.Info("image mirror agrees with its release")
		return nil
	}
	slog.Warn("image mirror disagrees with its release", "problems", len(problems))
	msg := "The scoreboard image mirror at images.scoreboard.davidjdrake.com disagrees with its GitHub release:\n\n- " +
		strings.Join(problems, "\n- ") +
		"\n\nIf this was not a release in progress, assume the image strangers download may have been replaced. " +
		"See HockeyTrack's docs/threat-model.md, section 7."
	return n.Notify(ctx, alertSubject, msg)
}
