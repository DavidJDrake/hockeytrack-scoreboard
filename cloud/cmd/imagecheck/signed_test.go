package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

// The signing key here is a throwaway generated in the test process; the
// real one lives in KMS and its private half never exists anywhere else.

type fakeKey struct {
	der []byte
	err error
}

func (k fakeKey) PublicKey(context.Context) ([]byte, error) { return k.der, k.err }

type fakeAssets struct {
	assets map[string][]byte
	err    error
}

func (f fakeAssets) Asset(_ context.Context, tag, file string) ([]byte, error) {
	if f.err != nil {
		return nil, f.err
	}
	b, ok := f.assets[tag+"/"+file]
	if !ok {
		return nil, ErrNoAsset
	}
	return b, nil
}

var (
	bootPayload = []byte("pretend this is a boot payload")
	rootPayload = []byte("pretend this is a root payload, a little longer")
	now         = time.Date(2026, 10, 10, 12, 0, 0, 0, time.UTC)
)

func newKey(t *testing.T) (*ecdsa.PrivateKey, []byte) {
	t.Helper()
	priv, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKIXPublicKey(&priv.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	return priv, der
}

func sign(t *testing.T, priv *ecdsa.PrivateKey, manifest []byte) []byte {
	t.Helper()
	digest := sha256.Sum256(manifest)
	sig, err := ecdsa.SignASN1(rand.Reader, priv, digest[:])
	if err != nil {
		t.Fatal(err)
	}
	return []byte(base64.StdEncoding.EncodeToString(sig) + "\n")
}

func signedManifest(version, channel, expires string) []byte {
	return []byte(fmt.Sprintf(`{"version":%q,"channel":%q,"released":"2026-10-03T02:11:09Z","expires":%q,"layout":1,`+
		`"payloads":{"boot":{"file":"scoreboard-%s.boot.img.xz","size":%d,"sha256":%q,"rawSize":268435456,"rawSha256":"%s"},`+
		`"root":{"file":"scoreboard-%s.root.img.xz","size":%d,"sha256":%q,"rawSize":3221225472,"rawSha256":"%s"}},"keyId":"release-2026-1"}`,
		version, channel, expires, version, len(bootPayload), sum(bootPayload), strings.Repeat("a", 64),
		version, len(rootPayload), sum(rootPayload), strings.Repeat("b", 64)))
}

// signedRelease is a mirror and a GitHub release that agree, with the
// manifest signed by a key KMS reports.
func signedRelease(t *testing.T) (*fakeObjects, fakeAssets, fakeKey, *ecdsa.PrivateKey) {
	priv, der := newKey(t)
	m := signedManifest("v0.2.0", "stable", "2027-01-31T02:11:09Z")
	objs := &fakeObjects{objects: map[string][]byte{
		"latest.json":                                   manifest("v0.2.0", "scoreboard-v0.2.0.img.xz", sum(image)),
		"images/v0.2.0/scoreboard-v0.2.0.img.xz":        image,
		"images/v0.2.0/scoreboard-v0.2.0.manifest.json": m,
		"images/v0.2.0/scoreboard-v0.2.0.manifest.sig":  sign(t, priv, m),
		"images/v0.2.0/scoreboard-v0.2.0.boot.img.xz":   bootPayload,
		"images/v0.2.0/scoreboard-v0.2.0.root.img.xz":   rootPayload,
	}}
	assets := fakeAssets{assets: map[string][]byte{"v0.2.0/scoreboard-v0.2.0.manifest.json": m}}
	return objs, assets, fakeKey{der: der}, priv
}

func TestASignedReleaseThatAgreesRaisesNothing(t *testing.T) {
	objs, assets, key, _ := signedRelease(t)
	problems, err := CheckSigned(context.Background(), objs, assets, key, now, "v0.2.0")
	if err != nil || len(problems) != 0 {
		t.Fatalf("problems %q, err %v", problems, err)
	}
}

func TestAReleaseFromBeforeManifestsExistedIsNotAProblem(t *testing.T) {
	objs, rel := agreeing()
	problems, err := CheckSigned(context.Background(), objs, fakeAssets{}, fakeKey{}, now, "v0.1.0")
	if err != nil || len(problems) != 0 {
		t.Fatalf("problems %q, err %v", problems, err)
	}
	_ = rel
}

func TestSignedProblems(t *testing.T) {
	cases := map[string]struct {
		mutate func(*fakeObjects, *fakeAssets, *fakeKey, *ecdsa.PrivateKey)
		want   string
	}{
		"the mirrored manifest was re-signed by another key": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			stranger, _ := newKey(t)
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.sig"] = sign(t, stranger, o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"])
		}, "does not verify against the KMS signing key"},
		"a byte of the mirrored manifest changed": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"] = append(o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"], ' ')
		}, "does not verify against the KMS signing key"},
		"the mirrored manifest differs from GitHub's": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, priv *ecdsa.PrivateKey) {
			m := signedManifest("v0.2.0", "stable", "2027-02-01T00:00:00Z")
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.sig"] = sign(t, priv, m)
		}, "differs from the GitHub release's"},
		"the signature object is missing": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			delete(o.objects, "images/v0.2.0/scoreboard-v0.2.0.manifest.sig")
		}, "no manifest signature"},
		"the mirror has no manifest but GitHub does": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			delete(o.objects, "images/v0.2.0/scoreboard-v0.2.0.manifest.json")
		}, "the mirror has no signed manifest"},
		"the mirror has a manifest but GitHub does not": {func(_ *fakeObjects, a *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			delete(a.assets, "v0.2.0/scoreboard-v0.2.0.manifest.json")
		}, "the GitHub release has none"},
		"the manifest names another version": {func(o *fakeObjects, a *fakeAssets, _ *fakeKey, priv *ecdsa.PrivateKey) {
			m := signedManifest("v0.3.0", "stable", "2027-01-31T02:11:09Z")
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.sig"] = sign(t, priv, m)
			a.assets["v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
		}, "names v0.3.0 but latest.json names v0.2.0"},
		"latest.json points at a test-channel manifest": {func(o *fakeObjects, a *fakeAssets, _ *fakeKey, priv *ecdsa.PrivateKey) {
			m := signedManifest("v0.2.0", "test", "2027-01-31T02:11:09Z")
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.sig"] = sign(t, priv, m)
			a.assets["v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
		}, "for the test channel"},
		"the manifest expires within 14 days": {func(o *fakeObjects, a *fakeAssets, _ *fakeKey, priv *ecdsa.PrivateKey) {
			m := signedManifest("v0.2.0", "stable", "2026-10-20T00:00:00Z")
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.sig"] = sign(t, priv, m)
			a.assets["v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
		}, "not a compromise, a deadline: the signed manifest for v0.2.0 expires on"},
		"the manifest has expired": {func(o *fakeObjects, a *fakeAssets, _ *fakeKey, priv *ecdsa.PrivateKey) {
			m := signedManifest("v0.2.0", "stable", "2026-10-01T00:00:00Z")
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
			o.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.sig"] = sign(t, priv, m)
			a.assets["v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
		}, "expired on 2026-10-01"},
		"the boot payload was replaced": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			o.objects["images/v0.2.0/scoreboard-v0.2.0.boot.img.xz"] = []byte("something else")
		}, "boot payload for v0.2.0 does not hash"},
		"the root payload was truncated": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			o.objects["images/v0.2.0/scoreboard-v0.2.0.root.img.xz"] = rootPayload[:10]
		}, "root payload for v0.2.0 does not hash"},
		"a payload object is missing": {func(o *fakeObjects, _ *fakeAssets, _ *fakeKey, _ *ecdsa.PrivateKey) {
			delete(o.objects, "images/v0.2.0/scoreboard-v0.2.0.root.img.xz")
		}, "no root payload"},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			objs, assets, key, priv := signedRelease(t)
			c.mutate(objs, &assets, &key, priv)
			problems, err := CheckSigned(context.Background(), objs, assets, key, now, "v0.2.0")
			if err != nil {
				t.Fatal(err)
			}
			if len(problems) == 0 || !strings.Contains(strings.Join(problems, "\n"), c.want) {
				t.Errorf("problems %q, want one containing %q", problems, c.want)
			}
		})
	}
}

func TestAManifestThatDoesNotVerifyIsNotParsedFurther(t *testing.T) {
	// Every later finding depends on the manifest's contents; if the
	// signature is bad, the contents are nobody's to reason about.
	objs, assets, key, _ := signedRelease(t)
	stranger, _ := newKey(t)
	m := signedManifest("v9.9.9", "test", "2020-01-01T00:00:00Z")
	objs.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
	objs.objects["images/v0.2.0/scoreboard-v0.2.0.manifest.sig"] = sign(t, stranger, m)
	assets.assets["v0.2.0/scoreboard-v0.2.0.manifest.json"] = m
	problems, err := CheckSigned(context.Background(), objs, assets, key, now, "v0.2.0")
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(problems, "\n")
	if !strings.Contains(joined, "does not verify") || strings.Contains(joined, "v9.9.9") || strings.Contains(joined, "expired") {
		t.Errorf("problems %q", problems)
	}
	for _, key := range objs.read {
		if strings.Contains(key, "payload") || strings.HasSuffix(key, ".boot.img.xz") || strings.HasSuffix(key, ".root.img.xz") {
			t.Errorf("read %s after a bad signature", key)
		}
	}
}

func TestAnUnreachableKMSIsAnErrorNotAnAlert(t *testing.T) {
	objs, assets, _, _ := signedRelease(t)
	_, err := CheckSigned(context.Background(), objs, assets, fakeKey{err: errors.New("kms down")}, now, "v0.2.0")
	if err == nil || !strings.Contains(err.Error(), "KMS") {
		t.Fatalf("err %v", err)
	}
}

func TestRunChecksTheSignedManifestForTheVersionLatestJsonNames(t *testing.T) {
	objs, assets, key, _ := signedRelease(t)
	rel := fakeReleases{latest: "v0.2.0", sums: map[string]string{"v0.2.0/scoreboard-v0.2.0.img.xz": sum(image)}}
	objs.objects["images/v0.2.0/scoreboard-v0.2.0.boot.img.xz"] = []byte("replaced")
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n, &Signing{Assets: assets, Key: key, Now: func() time.Time { return now }}); err != nil {
		t.Fatal(err)
	}
	if len(n.messages) != 1 || !strings.Contains(n.messages[0], "boot payload for v0.2.0 does not hash") {
		t.Errorf("messages %q", n.messages)
	}
	if !strings.Contains(n.messages[0], "the update panels will install") {
		t.Errorf("the alert does not say what is at stake: %q", n.messages[0])
	}
}

func TestRunDoesNotCheckSignaturesWhenLatestJsonIsAlreadyUnusable(t *testing.T) {
	objs, assets, key, _ := signedRelease(t)
	objs.objects["latest.json"] = []byte("<html>")
	rel := fakeReleases{latest: "v0.2.0"}
	n := &fakeNotifier{}
	if err := Run(context.Background(), objs, rel, n, &Signing{Assets: assets, Key: key, Now: func() time.Time { return now }}); err != nil {
		t.Fatal(err)
	}
	for _, key := range objs.read {
		if strings.Contains(key, "manifest") {
			t.Errorf("read %s although latest.json named no version", key)
		}
	}
}
