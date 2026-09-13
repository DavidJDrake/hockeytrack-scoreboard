// Package enroll turns a freshly flashed panel into a registered device: it
// mints the short code a person reads off the screen, the long token the
// device proves itself with, and the name the thing will carry.
package enroll

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"math/big"
	"strings"
)

// Alphabet is Crockford base32 without the characters people mistype off a
// screen: 0 and O, 1 and I and L. U is out too, as Crockford has it, so a
// random draw cannot spell something unfortunate.
const Alphabet = "23456789ABCDEFGHJKMNPQRSTVWXYZ"

const (
	codeLength      = 8
	thingSuffixSize = 12
	tokenBytes      = 32
)

func randomString(n int) (string, error) {
	limit := big.NewInt(int64(len(Alphabet)))
	out := make([]byte, n)
	for i := range out {
		// crypto/rand.Int is uniform over [0, limit) -- no modulo bias, which
		// a plain "read a byte and take it mod 30" would introduce.
		pick, err := rand.Int(rand.Reader, limit)
		if err != nil {
			return "", err
		}
		out[i] = Alphabet[pick.Int64()]
	}
	return string(out), nil
}

// Code is the pairing code shown on the panel. Short enough to read across a
// room, and only useful while the enrollment it belongs to is unclaimed.
func Code() (string, error) { return randomString(codeLength) }

// Token is the device's proof that it is the panel that asked to enroll. It
// is never displayed and never typed: the pairing code is on a screen anyone
// in the room can see, so the code alone must not be enough to collect a
// certificate.
func Token() (string, error) {
	raw := make([]byte, tokenBytes)
	if _, err := rand.Read(raw); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(raw), nil
}

// ThingName is assigned here rather than taken from the CSR, so that nothing
// a caller sends can name or collide with an existing device.
func ThingName() (string, error) {
	suffix, err := randomString(thingSuffixSize)
	if err != nil {
		return "", err
	}
	return "scoreboard-" + strings.ToLower(suffix), nil
}

// HashSecret is what gets stored. A dump of the enrollments table then yields
// nobody a usable collection token and nobody a claimable code -- the same
// reasoning as not storing passwords, applied to two short-lived secrets that
// would otherwise sit in plaintext beside the certificates they unlock.
func HashSecret(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// NormalizeCode accepts what a person actually types: any case, with or
// without the separator, with stray whitespace.
func NormalizeCode(input string) string {
	var b strings.Builder
	for _, r := range strings.ToUpper(input) {
		if strings.ContainsRune(Alphabet, r) {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// NormalizeOwner puts a hand-typed owner hint and a JWT claim into the same
// shape so they can be compared. Case and surrounding whitespace only: plus
// addressing and dots are meaningful to Cognito, so they are meaningful here.
func NormalizeOwner(input string) string {
	return strings.ToLower(strings.TrimSpace(input))
}

// FormatCode splits the code for display. Four and four is easier to hold in
// your head while you walk to a laptop.
func FormatCode(code string) string {
	if len(code) != codeLength {
		return code
	}
	return code[:4] + "-" + code[4:]
}
