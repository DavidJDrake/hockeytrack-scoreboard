package enroll

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
)

// MaxCSRBytes caps the request body. A P-256 CSR is a few hundred bytes; this
// leaves generous room while refusing anything sent to waste our time.
const MaxCSRBytes = 4096

// ParseCSR validates a certificate signing request from an unauthenticated
// caller.
//
// Note what it does NOT do: it never reads the subject, the SANs, or any
// extension, and the caller must not either. A CSR here is a public key plus a
// proof of possession. It is not a request for a name -- the thing name is
// assigned by ThingName(), server-side, so that nothing a stranger sends can
// name a device or collide with one that exists.
//
// The signature is error-only so the subject cannot escape to the caller.
func ParseCSR(pemBytes []byte) error {
	if len(pemBytes) == 0 {
		return errors.New("empty certificate request")
	}
	if len(pemBytes) > MaxCSRBytes {
		return fmt.Errorf("certificate request is %d bytes; the maximum is %d", len(pemBytes), MaxCSRBytes)
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil || block.Type != "CERTIFICATE REQUEST" {
		return errors.New("not a PEM certificate request")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		// Deliberately not wrapped: x509's errors can quote parts of the
		// input, and the input is attacker-supplied.
		return errors.New("malformed certificate request")
	}
	// The signature is the sender's proof it holds the matching private key.
	// Without this check anyone could enroll a public key they found.
	if err := csr.CheckSignature(); err != nil {
		return errors.New("certificate request signature does not verify")
	}
	pub, ok := csr.PublicKey.(*ecdsa.PublicKey)
	if !ok {
		return errors.New("key must be ECDSA P-256")
	}
	if pub.Curve != elliptic.P256() {
		return errors.New("key must be ECDSA P-256")
	}
	return nil
}
