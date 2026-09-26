package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// GitHub reads a public repository's releases without credentials. Its
// unauthenticated API rate limit is 60 requests an hour; each run makes one API
// request and one release-asset download, twice a day, so four requests a day.
type GitHub struct {
	Repo   string // owner/name
	API    string // https://api.github.com
	Web    string // https://github.com
	Client *http.Client
}

// allowedRedirectHosts are the only hosts a github.com or api.github.com
// request may legitimately be redirected to: github.com and
// api.github.com themselves (a renamed or transferred repository 301s
// api.github.com/repos/<old>/... to api.github.com/repositories/<id>/...),
// and the two hosts release assets are actually served from.
// redirectPolicy is meant to be set as an http.Client's CheckRedirect, so a
// captive proxy, DNS hijack or compromised intermediary cannot make this
// function fetch a "checksum" -- or anything else -- from an attacker's
// server just by 30x-ing a request there.
var allowedRedirectHosts = map[string]bool{
	"github.com":                           true,
	"api.github.com":                       true,
	"release-assets.githubusercontent.com": true,
	"objects.githubusercontent.com":        true,
}

func redirectPolicy(req *http.Request, via []*http.Request) error {
	if len(via) >= 5 {
		return errors.New("stopped after 5 redirects")
	}
	if req.URL.Scheme != "https" {
		return fmt.Errorf("refusing a redirect to a non-https URL: %s", req.URL)
	}
	if !allowedRedirectHosts[req.URL.Hostname()] {
		return fmt.Errorf("refusing a redirect to an unexpected host: %s", req.URL.Hostname())
	}
	return nil
}

func (g GitHub) get(ctx context.Context, url string, limit int64) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "hockeytrack-scoreboard-imagecheck (+https://github.com/DavidJDrake/hockeytrack-scoreboard)")
	resp, err := g.Client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, limit))
	return body, resp.StatusCode, err
}

func (g GitHub) LatestTag(ctx context.Context) (string, error) {
	body, status, err := g.get(ctx, g.API+"/repos/"+g.Repo+"/releases/latest", 1<<20)
	if err != nil {
		return "", err
	}
	if status == http.StatusNotFound {
		return "", ErrNoRelease
	}
	if status != http.StatusOK {
		return "", fmt.Errorf("latest release: status %d", status)
	}
	var out struct {
		Tag string `json:"tag_name"`
	}
	if err := json.Unmarshal(body, &out); err != nil {
		return "", fmt.Errorf("latest release: %w", err)
	}
	if !versionRE.MatchString(out.Tag) {
		return "", errors.New("latest release has an unexpected tag")
	}
	return out.Tag, nil
}

// Asset downloads one small release file, such as the signed manifest, so
// the mirror's copy can be compared to the source of truth byte for byte.
func (g GitHub) Asset(ctx context.Context, tag, file string) ([]byte, error) {
	body, status, err := g.get(ctx, g.Web+"/"+g.Repo+"/releases/download/"+tag+"/"+file, maxManifest)
	if err != nil {
		return nil, err
	}
	if status == http.StatusNotFound {
		return nil, ErrNoAsset
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("asset %s for %s: status %d", file, tag, status)
	}
	return body, nil
}

func (g GitHub) Checksum(ctx context.Context, tag, file string) (string, error) {
	body, status, err := g.get(ctx, g.Web+"/"+g.Repo+"/releases/download/"+tag+"/"+file+".sha256", 4096)
	if err != nil {
		return "", err
	}
	if status == http.StatusNotFound {
		return "", ErrNoChecksum
	}
	if status != http.StatusOK {
		return "", fmt.Errorf("checksum for %s: status %d", tag, status)
	}
	fields := strings.Fields(string(body))
	if len(fields) != 2 || !hexRE.MatchString(fields[0]) || fields[1] != file {
		return "", fmt.Errorf("checksum for %s is not a sha256sum line for %s", tag, file)
	}
	return fields[0], nil
}
