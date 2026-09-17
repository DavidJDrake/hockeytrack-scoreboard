package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

type fakeObjects struct {
	objects map[string][]byte
	// errs, keyed by object key, lets a test simulate a non-404 read
	// failure (a real outage) distinct from a missing object.
	errs map[string]error
	read []string
}

func (f *fakeObjects) Get(_ context.Context, key string) (io.ReadCloser, error) {
	f.read = append(f.read, key)
	if err, ok := f.errs[key]; ok {
		return nil, err
	}
	b, ok := f.objects[key]
	if !ok {
		return nil, ErrNotFound
	}
	return io.NopCloser(bytes.NewReader(b)), nil
}

type fakeReleases struct {
	latest    string
	latestErr error
	sums      map[string]string
	sumErr    error
}

func (f fakeReleases) LatestTag(context.Context) (string, error) { return f.latest, f.latestErr }

// Checksum mirrors the real GitHub client: a tag/file combination that was
// never published 404s (ErrNoChecksum), it does not silently answer "".
func (f fakeReleases) Checksum(_ context.Context, tag, file string) (string, error) {
	if f.sumErr != nil {
		return "", f.sumErr
	}
	sum, ok := f.sums[tag+"/"+file]
	if !ok {
		return "", ErrNoChecksum
	}
	return sum, nil
}

type fakeNotifier struct{ subjects, messages []string }

func (f *fakeNotifier) Notify(_ context.Context, subject, message string) error {
	f.subjects = append(f.subjects, subject)
	f.messages = append(f.messages, message)
	return nil
}

var image = []byte("pretend this is an xz image")

func sum(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

func manifest(version, file, sha string) []byte {
	return []byte(`{"version":"` + version + `","file":"` + file + `","sha256":"` + sha + `","size":27,"released":"2026-09-16T00:00:00Z","release":"https://github.com/x"}`)
}

// manifestSized is manifest with an independently chosen size, for testing
// a latest.json whose size field was rewritten but whose version, file and
// checksum are otherwise legitimate.
func manifestSized(version, file, sha string, size int) []byte {
	return []byte(fmt.Sprintf(`{"version":%q,"file":%q,"sha256":%q,"size":%d,"released":"2026-09-16T00:00:00Z","release":"https://github.com/x"}`, version, file, sha, size))
}

func agreeing() (*fakeObjects, fakeReleases) {
	objs := &fakeObjects{objects: map[string][]byte{
		"latest.json":                            manifest("v0.1.0", "scoreboard-v0.1.0.img.xz", sum(image)),
		"images/v0.1.0/scoreboard-v0.1.0.img.xz": image,
	}}
	rel := fakeReleases{latest: "v0.1.0", sums: map[string]string{"v0.1.0/scoreboard-v0.1.0.img.xz": sum(image)}}
	return objs, rel
}

func TestAMirrorThatAgreesRaisesNothing(t *testing.T) {
	objs, rel := agreeing()
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n); err != nil {
		t.Fatal(err)
	}
	if len(n.subjects) != 0 {
		t.Errorf("notified on agreement: %v", n.messages)
	}
}

func TestNothingPublishedYetIsNotAProblem(t *testing.T) {
	n := &fakeNotifier{}
	err := Run(context.Background(), &fakeObjects{objects: map[string][]byte{}}, fakeReleases{latestErr: ErrNoRelease}, n)
	if err != nil || len(n.subjects) != 0 {
		t.Errorf("err %v, notified %v", err, n.messages)
	}
}

func TestProblems(t *testing.T) {
	tampered := []byte("a different image")
	cases := map[string]struct {
		mutate func(*fakeObjects, *fakeReleases)
		want   string
	}{
		"the mirrored image was replaced": {func(o *fakeObjects, _ *fakeReleases) {
			o.objects["images/v0.1.0/scoreboard-v0.1.0.img.xz"] = tampered
		}, "does not hash to the GitHub release's checksum"},
		"latest.json's checksum was rewritten": {func(o *fakeObjects, _ *fakeReleases) {
			o.objects["latest.json"] = manifest("v0.1.0", "scoreboard-v0.1.0.img.xz", sum(tampered))
		}, "differs from the GitHub release's"},
		"the mirror is behind the latest release": {func(_ *fakeObjects, r *fakeReleases) {
			r.latest = "v0.2.0"
		}, "latest GitHub release is v0.2.0"},
		"a release exists but the mirror has no manifest": {func(o *fakeObjects, _ *fakeReleases) {
			delete(o.objects, "latest.json")
		}, "has no latest.json"},
		"latest.json is not JSON": {func(o *fakeObjects, _ *fakeReleases) {
			o.objects["latest.json"] = []byte("<html>")
		}, "not a valid manifest"},
		"the mirror has no image object at all": {func(o *fakeObjects, _ *fakeReleases) {
			delete(o.objects, "images/v0.1.0/scoreboard-v0.1.0.img.xz")
		}, "the mirror has no image object for v0.1.0"},
		"latest.json exists but GitHub has no release at all": {func(_ *fakeObjects, r *fakeReleases) {
			r.latest = ""
			r.latestErr = ErrNoRelease
		}, "GitHub has no release at all"},
		"latest.json's size does not match the object": {func(o *fakeObjects, _ *fakeReleases) {
			o.objects["latest.json"] = manifestSized("v0.1.0", "scoreboard-v0.1.0.img.xz", sum(image), 999)
		}, "is 27 bytes but latest.json says 999 bytes"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			objs, rel := agreeing()
			c.mutate(objs, &rel)
			problems, err := Check(context.Background(), objs, rel)
			if err != nil {
				t.Fatal(err)
			}
			if len(problems) == 0 || !strings.Contains(strings.Join(problems, "\n"), c.want) {
				t.Errorf("problems %q, want one containing %q", problems, c.want)
			}
		})
	}
}

func TestAManifestNamingAnotherObjectIsRefusedWithoutReadingIt(t *testing.T) {
	objs, rel := agreeing()
	objs.objects["latest.json"] = manifest("v0.1.0", "../../site/index.html", sum(image))
	problems, err := Check(context.Background(), objs, rel)
	if err != nil {
		t.Fatal(err)
	}
	if len(problems) != 1 || !strings.Contains(problems[0], "unexpected version, file or checksum") {
		t.Fatalf("problems %q", problems)
	}
	for _, key := range objs.read {
		if key != "latest.json" {
			t.Errorf("read %q after an invalid manifest", key)
		}
	}
}

func TestAnUnreachableGitHubIsAnErrorNotAnAlert(t *testing.T) {
	objs, rel := agreeing()
	rel.latestErr = errors.New("connection reset")
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n); err == nil {
		t.Fatal("want an error so the function's error alarm fires")
	}
	if len(n.subjects) != 0 {
		t.Error("an outage was reported as tampering")
	}
}

func TestRunNamesEveryProblemInOneAlert(t *testing.T) {
	objs, rel := agreeing()
	objs.objects["images/v0.1.0/scoreboard-v0.1.0.img.xz"] = []byte("x")
	rel.latest = "v0.2.0"
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n); err != nil {
		t.Fatal(err)
	}
	if len(n.subjects) != 1 || n.subjects[0] != alertSubject {
		t.Fatalf("subjects %q", n.subjects)
	}
	for _, want := range []string{"does not hash", "latest GitHub release is v0.2.0"} {
		if !strings.Contains(n.messages[0], want) {
			t.Errorf("message lacks %q:\n%s", want, n.messages[0])
		}
	}
}

func TestGitHubReadsTheLatestTagAndItsChecksum(t *testing.T) {
	good := sum(image)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/repos/DavidJDrake/hockeytrack-scoreboard/releases/latest":
			w.Write([]byte(`{"tag_name":"v0.1.0"}`))
		case "/DavidJDrake/hockeytrack-scoreboard/releases/download/v0.1.0/scoreboard-v0.1.0.img.xz.sha256":
			w.Write([]byte(good + "  scoreboard-v0.1.0.img.xz\n"))
		case "/DavidJDrake/hockeytrack-scoreboard/releases/download/v0.1.0/wrong.img.xz.sha256":
			w.Write([]byte(good + "  scoreboard-v0.1.0.img.xz\n"))
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	g := GitHub{Repo: "DavidJDrake/hockeytrack-scoreboard", API: srv.URL, Web: srv.URL, Client: srv.Client()}
	ctx := context.Background()

	if tag, err := g.LatestTag(ctx); err != nil || tag != "v0.1.0" {
		t.Errorf("LatestTag = %q, %v", tag, err)
	}
	if got, err := g.Checksum(ctx, "v0.1.0", "scoreboard-v0.1.0.img.xz"); err != nil || got != good {
		t.Errorf("Checksum = %q, %v", got, err)
	}
	if _, err := g.Checksum(ctx, "v0.1.0", "wrong.img.xz"); err == nil {
		t.Error("a checksum file naming another file was accepted")
	}
	if _, err := g.Checksum(ctx, "v9.9.9", "scoreboard-v9.9.9.img.xz"); err == nil {
		t.Error("a missing checksum file was accepted")
	}
}

func TestGitHubWithNoReleasesSaysSo(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	defer srv.Close()
	g := GitHub{Repo: "DavidJDrake/hockeytrack-scoreboard", API: srv.URL, Web: srv.URL, Client: srv.Client()}
	if _, err := g.LatestTag(context.Background()); !errors.Is(err, ErrNoRelease) {
		t.Errorf("err = %v, want ErrNoRelease", err)
	}
}

func TestGitHubForbiddenOrRateLimitedIsAnErrorNotNoRelease(t *testing.T) {
	for _, status := range []int{http.StatusForbidden, http.StatusTooManyRequests} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(status)
			}))
			defer srv.Close()
			g := GitHub{Repo: "DavidJDrake/hockeytrack-scoreboard", API: srv.URL, Web: srv.URL, Client: srv.Client()}
			_, err := g.LatestTag(context.Background())
			if err == nil {
				t.Fatal("want an error")
			}
			if errors.Is(err, ErrNoRelease) {
				t.Errorf("a %d was treated as ErrNoRelease, which means no notification and no error alarm", status)
			}
		})
	}
}

// The redirect target is a second, real local server, not an unresolvable
// hostname: a bare DNS failure would make this test pass even with no
// policy at all. It is also a TLS server, and the client is built from its
// own Client() (which trusts its certificate), so a follow is prevented
// only by redirectPolicy -- never incidentally by certificate distrust,
// which would likewise pass for the wrong reason. (Confirmed by hand: with
// CheckRedirect removed, this same setup follows the redirect and
// evilRequests becomes 1 -- see the fix report.)
func TestGitHubRefusesARedirectToAnotherHost(t *testing.T) {
	var evilRequests int
	evil := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		evilRequests++
		w.Write([]byte(sum(image) + "  scoreboard-v0.1.0.img.xz\n"))
	}))
	defer evil.Close()

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, evil.URL+"/scoreboard-v0.1.0.img.xz.sha256", http.StatusFound)
	}))
	defer srv.Close()

	client := &http.Client{
		Transport:     evil.Client().Transport,
		CheckRedirect: redirectPolicy,
	}
	g := GitHub{Repo: "DavidJDrake/hockeytrack-scoreboard", API: srv.URL, Web: srv.URL, Client: client}

	_, err := g.Checksum(context.Background(), "v0.1.0", "scoreboard-v0.1.0.img.xz")
	if err == nil {
		t.Fatal("a redirect to an unexpected host was followed instead of refused")
	}
	evilHost := mustHostname(t, evil.URL)
	if !strings.Contains(err.Error(), evilHost) {
		t.Errorf("error %q does not name the refused host %q", err, evilHost)
	}
	if evilRequests != 0 {
		t.Errorf("the redirect was followed: the other server received %d requests", evilRequests)
	}
}

func mustHostname(t *testing.T, rawURL string) string {
	t.Helper()
	u, err := url.Parse(rawURL)
	if err != nil {
		t.Fatalf("parsing %q: %v", rawURL, err)
	}
	return u.Hostname()
}

// A forged version is not merely an infrastructure error: latest.json names
// a version GitHub never released, and the fake's Checksum 404s exactly
// like the real client would. This used to discard every problem already
// found (including the version mismatch itself) and surface only as an
// error, so the errors alarm -- whose own description says a GitHub outage
// clears itself on the next run -- was the only signal, and nobody was
// told the mirror might have been tampered with.
func TestAVersionGitHubDoesNotHaveIsReportedAsTampering(t *testing.T) {
	objs, rel := agreeing()
	objs.objects["latest.json"] = manifest("v9.9.9", "scoreboard-v9.9.9.img.xz", sum(image))
	objs.objects["images/v9.9.9/scoreboard-v9.9.9.img.xz"] = image
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n); err != nil {
		t.Fatal(err)
	}
	if len(n.subjects) != 1 || n.subjects[0] != alertSubject {
		t.Fatalf("subjects %q, want exactly one alert", n.subjects)
	}
	for _, want := range []string{"latest GitHub release is v0.1.0", "no release checksum for v9.9.9"} {
		if !strings.Contains(n.messages[0], want) {
			t.Errorf("message lacks %q:\n%s", want, n.messages[0])
		}
	}
}

// A problem found early (the version mismatch) must still be published even
// when a later step -- here, reading the image object -- fails outright,
// rather than the whole check being discarded as a bare error.
func TestProblemsFoundSoFarArePublishedEvenIfALaterStepErrors(t *testing.T) {
	objs, rel := agreeing()
	rel.latest = "v0.2.0"
	objs.errs = map[string]error{"images/v0.1.0/scoreboard-v0.1.0.img.xz": errors.New("connection reset")}
	n := &fakeNotifier{}
	err := Run(context.Background(), objs, rel, n)
	if err == nil {
		t.Fatal("want an error so the function's error alarm also fires")
	}
	if len(n.subjects) != 1 || n.subjects[0] != alertSubject {
		t.Fatalf("subjects %q, want the alert published despite the later error", n.subjects)
	}
	if !strings.Contains(n.messages[0], "latest GitHub release is v0.2.0") {
		t.Errorf("message lacks the problem already found:\n%s", n.messages[0])
	}
}
