package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type fakeObjects struct {
	objects map[string][]byte
	read    []string
}

func (f *fakeObjects) Get(_ context.Context, key string) (io.ReadCloser, error) {
	f.read = append(f.read, key)
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
func (f fakeReleases) Checksum(_ context.Context, tag, file string) (string, error) {
	if f.sumErr != nil {
		return "", f.sumErr
	}
	return f.sums[tag+"/"+file], nil
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
