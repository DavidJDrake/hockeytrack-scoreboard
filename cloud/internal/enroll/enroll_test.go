package enroll

import (
	"strings"
	"testing"
)

func TestAlphabetExcludesAmbiguousCharacters(t *testing.T) {
	// Typed off a screen from across a room. 0/O and 1/I/L are the pairs
	// people get wrong, and a wrong character is indistinguishable from an
	// expired code to the person typing it.
	for _, bad := range []string{"0", "O", "1", "I", "L", "U"} {
		if strings.Contains(Alphabet, bad) {
			t.Errorf("alphabet contains ambiguous %q", bad)
		}
	}
	if len(Alphabet) != 30 {
		t.Errorf("alphabet is %d characters, want 30", len(Alphabet))
	}
}

func TestCodeShape(t *testing.T) {
	code, err := Code()
	if err != nil {
		t.Fatal(err)
	}
	if len(code) != 8 {
		t.Fatalf("code %q is %d characters, want 8", code, len(code))
	}
	for _, r := range code {
		if !strings.ContainsRune(Alphabet, r) {
			t.Errorf("code %q contains %q, which is not in the alphabet", code, r)
		}
	}
}

func TestCodesDiffer(t *testing.T) {
	seen := map[string]bool{}
	for i := 0; i < 200; i++ {
		code, err := Code()
		if err != nil {
			t.Fatal(err)
		}
		if seen[code] {
			t.Fatalf("Code() repeated %q within 200 draws", code)
		}
		seen[code] = true
	}
}

func TestTokenIsLongAndUnpredictable(t *testing.T) {
	a, err := Token()
	if err != nil {
		t.Fatal(err)
	}
	b, _ := Token()
	if a == b {
		t.Fatal("two tokens were identical")
	}
	// 32 bytes base64url, unpadded.
	if len(a) < 43 {
		t.Fatalf("token %q is only %d characters", a, len(a))
	}
}

func TestThingNameIsPrefixedAndSafe(t *testing.T) {
	name, err := ThingName()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(name, "scoreboard-") {
		t.Fatalf("thing name %q lacks the scoreboard- prefix", name)
	}
	// AWS IoT accepts [a-zA-Z0-9:_-]; the devices package is stricter still.
	for _, r := range name {
		ok := r == '-' || (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')
		if !ok {
			t.Errorf("thing name %q contains %q", name, r)
		}
	}
}

func TestHashSecretIsStableAndHidesTheInput(t *testing.T) {
	h := HashSecret("ABCD2345")
	if h == "ABCD2345" {
		t.Fatal("HashSecret returned its input")
	}
	if h != HashSecret("ABCD2345") {
		t.Fatal("HashSecret is not stable")
	}
	if len(h) != 64 {
		t.Fatalf("hash %q is %d characters, want 64 hex", h, len(h))
	}
}

func TestNormalizeCodeAcceptsWhatPeopleActuallyType(t *testing.T) {
	for _, in := range []string{"abcd2345", "ABCD-2345", "abcd 2345", " ABCD-2345 ", "AbCd-2345"} {
		if got := NormalizeCode(in); got != "ABCD2345" {
			t.Errorf("NormalizeCode(%q) = %q, want ABCD2345", in, got)
		}
	}
}

func TestFormatCodeIsReadableOnAPanel(t *testing.T) {
	if got := FormatCode("ABCD2345"); got != "ABCD-2345" {
		t.Errorf("FormatCode = %q, want ABCD-2345", got)
	}
}

func TestNormalizeOwnerMatchesHandTypedAddresses(t *testing.T) {
	// The owner hint is typed into a file on a FAT partition by a person, and
	// compared against a claim in a JWT. Both sides go through this, so a
	// trailing space or a capital letter cannot lock somebody out of their own
	// panel.
	for _, in := range []string{"Friend@Example.com", " friend@example.com ", "FRIEND@EXAMPLE.COM", "friend@example.com\n"} {
		if got := NormalizeOwner(in); got != "friend@example.com" {
			t.Errorf("NormalizeOwner(%q) = %q", in, got)
		}
	}
}

func TestNormalizeOwnerDoesNotStripPlusAddressing(t *testing.T) {
	// friend+panel@example.com is a different address from friend@example.com
	// as far as Cognito is concerned, so it must stay different here.
	if got := NormalizeOwner("Friend+Panel@example.com"); got != "friend+panel@example.com" {
		t.Errorf("got %q", got)
	}
}
