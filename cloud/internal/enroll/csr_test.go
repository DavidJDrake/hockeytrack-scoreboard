package enroll

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"strings"
	"testing"
)

func makeCSR(t *testing.T, key any, subject string) []byte {
	t.Helper()
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: subject},
	}, key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der})
}

func p256(t *testing.T) *ecdsa.PrivateKey {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func TestParseCSRAcceptsAP256Request(t *testing.T) {
	if _, err := ParseCSR(makeCSR(t, p256(t), "whatever")); err != nil {
		t.Fatalf("a valid P-256 CSR was rejected: %v", err)
	}
}

func TestParseCSRRejectsAnOversizedBody(t *testing.T) {
	huge := make([]byte, MaxCSRBytes+1)
	if _, err := ParseCSR(huge); err == nil {
		t.Fatal("an oversized body was accepted")
	}
}

func TestParseCSRRejectsRubbish(t *testing.T) {
	for name, body := range map[string][]byte{
		"empty":        {},
		"not pem":      []byte("hello"),
		"wrong block":  pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: []byte{1, 2, 3}}),
		"bad der":      pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: []byte{1, 2, 3}}),
	} {
		if _, err := ParseCSR(body); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestParseCSRRejectsRSA(t *testing.T) {
	// Not taste: an attacker who can pick the algorithm can pick an expensive
	// one and bill us for the CPU.
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseCSR(makeCSR(t, key, "rsa")); err == nil {
		t.Fatal("an RSA CSR was accepted")
	}
}

func TestParseCSRRejectsTheWrongCurve(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ParseCSR(makeCSR(t, key, "p384")); err == nil {
		t.Fatal("a P-384 CSR was accepted")
	}
}

func TestParseCSRRejectsABrokenSignature(t *testing.T) {
	// The signature is the sender's proof that it holds the private key. A CSR
	// that does not verify is somebody replaying a public key they found.
	body := makeCSR(t, p256(t), "tampered")
	block, _ := pem.Decode(body)
	block.Bytes[len(block.Bytes)-1] ^= 0xff
	if _, err := ParseCSR(pem.EncodeToMemory(block)); err == nil {
		t.Fatal("a CSR with a broken signature was accepted")
	}
}

func TestParseCSRDoesNotLeakTheSubjectIntoTheError(t *testing.T) {
	// Nothing downstream may use the subject, and an error message is the
	// easiest place for it to escape into a log.
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	_, parseErr := ParseCSR(makeCSR(t, key, "scoreboard-victim"))
	if parseErr == nil {
		t.Fatal("expected rejection")
	}
	if strings.Contains(parseErr.Error(), "scoreboard-victim") {
		t.Errorf("the CSR subject reached the error message: %v", parseErr)
	}
}
