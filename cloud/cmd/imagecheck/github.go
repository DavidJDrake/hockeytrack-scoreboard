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
// unauthenticated rate limit is 60 requests an hour; this makes two a day.
type GitHub struct {
	Repo   string // owner/name
	API    string // https://api.github.com
	Web    string // https://github.com
	Client *http.Client
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

func (g GitHub) Checksum(ctx context.Context, tag, file string) (string, error) {
	body, status, err := g.get(ctx, g.Web+"/"+g.Repo+"/releases/download/"+tag+"/"+file+".sha256", 4096)
	if err != nil {
		return "", err
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
