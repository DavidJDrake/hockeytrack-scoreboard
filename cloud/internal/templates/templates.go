// Package templates keeps an account's templates: named sets of games that
// any number of the account's panels may share (design section 4, SCO-40).
//
// Every operation takes the owner, and the store's key is (owner, id) with
// the owner as the hash key: there is no way to ask for a template by its id
// alone, so an ownership check is not something a caller can forget. A
// template that is not the caller's is one the store never lists for them,
// and the API turns that absence into a 404, never a 403.
//
// A template carries no overlap resolutions of its own. The same template
// can sit beside different games on different panels, so the resolution that
// counts is the panel's (internal/schedule).
package templates

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"sort"
	"sync"
	"unicode"
	"unicode/utf8"
)

// Bounds, from the design (section 8). They are what keeps an account's
// templates, and the work a panel's schedule does with them, a known size.
const (
	// MaxPerAccount is how many templates one account may hold. A panel takes
	// at most five (schedule.MaxTemplates); twenty is room for several panels
	// with different tastes.
	MaxPerAccount = 20
	// MaxGames is the most games in one template: the same bound as a panel's
	// own list, so a panel with every template attached is still bounded.
	MaxGames = 1500
	// MaxNameRunes bounds a name: 1 to 40 characters, every one printable.
	// Names are shown on the site and nowhere else; no control characters,
	// so a name cannot carry a terminal escape or a line break into a log.
	MaxNameRunes = 40
	// MaxBody bounds a request: 1,500 ten-digit ids and a name fit in 64 KB
	// with room to spare. Checked before anything is parsed.
	MaxBody = 64 << 10
	// idBytes is the entropy in a server-made id: 12 bytes, 24 hex characters.
	// Enough that two accounts never collide by chance, and a collision
	// within one account is refused by the store's condition, not overwritten.
	idBytes = 12
)

var (
	// ErrMalformed covers everything wrong with the request itself.
	ErrMalformed = errors.New("templates: malformed")
	// ErrBadName is a name outside the bound or with a character that is
	// not printable.
	ErrBadName = errors.New("templates: invalid name")
	// ErrNotFound is a template the owner does not hold. Not-yours and
	// not-there are the same error on purpose.
	ErrNotFound = errors.New("templates: not found")
	// ErrExists is Create finding the id already taken under this owner.
	ErrExists = errors.New("templates: id already exists")
)

// Template is one named set of games. ID is the server's and is never taken
// from a client.
type Template struct {
	ID    string  `json:"id"`
	Name  string  `json:"name"`
	Games []int64 `json:"games"`
}

// Decode reads a request body strictly: unknown keys, trailing data, too
// many games, an id that is not positive or appears twice are ErrMalformed;
// a name outside its bound is ErrBadName. The result carries no ID: an id is
// the server's to make, and one in the body is an unknown key.
func Decode(body []byte) (Template, error) {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var in struct {
		Name  string  `json:"name"`
		Games []int64 `json:"games"`
	}
	if err := dec.Decode(&in); err != nil || dec.More() {
		return Template{}, ErrMalformed
	}
	if len(in.Games) > MaxGames || !distinctPositive(in.Games) {
		return Template{}, ErrMalformed
	}
	if !ValidName(in.Name) {
		return Template{}, ErrBadName
	}
	t := Template{Name: in.Name, Games: in.Games}
	if t.Games == nil {
		t.Games = []int64{}
	}
	return t, nil
}

// ValidName reports whether name is 1 to MaxNameRunes printable characters
// with at least one that is not a space.
func ValidName(name string) bool {
	if !utf8.ValidString(name) {
		return false
	}
	n, blank := 0, true
	for _, r := range name {
		if !unicode.IsPrint(r) {
			return false
		}
		if !unicode.IsSpace(r) {
			blank = false
		}
		n++
	}
	return n >= 1 && n <= MaxNameRunes && !blank
}

func distinctPositive(ids []int64) bool {
	seen := make(map[int64]bool, len(ids))
	for _, id := range ids {
		if id <= 0 || seen[id] {
			return false
		}
		seen[id] = true
	}
	return true
}

// NewID makes a template id: idBytes of randomness as lowercase hex. It
// panics if the system's randomness is unavailable, because an id made any
// other way would be one a client could guess, and a guessable id is not a
// leak here (every read is under the owner) but it is not the design either.
func NewID() string {
	b := make([]byte, idBytes)
	if _, err := rand.Read(b); err != nil {
		panic("templates: randomness unavailable: " + err.Error())
	}
	return hex.EncodeToString(b)
}

// GamesOf indexes a list by id: the shape schedule.Candidates and
// schedule.Save take. A map built from one owner's list holds only that
// owner's templates, which is what makes an id in it one the caller may use.
func GamesOf(list []Template) map[string][]int64 {
	out := make(map[string][]int64, len(list))
	for _, t := range list {
		out[t.ID] = t.Games
	}
	return out
}

// Store persists Templates. Every method takes the owner, and every read is
// by owner: there is no Get by id.
type Store interface {
	// List returns every template the owner holds, by name then id. An owner
	// with none gets an empty list, which is not an error.
	List(ctx context.Context, owner string) ([]Template, error)
	// Create stores a new template under owner. Returns ErrExists if the
	// owner already holds one with this id; the caller made the id and
	// should make another.
	Create(ctx context.Context, owner string, t Template) error
	// Replace overwrites a template the owner holds. Returns ErrNotFound if
	// they hold none with this id: a Replace never creates, so a client
	// cannot choose an id.
	Replace(ctx context.Context, owner string, t Template) error
	// Delete removes a template the owner holds. Returns ErrNotFound if they
	// hold none with this id.
	Delete(ctx context.Context, owner, id string) error
}

// Fake is an in-memory Store for tests.
type Fake struct {
	mu    sync.Mutex
	items map[string]map[string]Template
	Err   error // returned by every call when set
}

// NewFake returns an empty Fake store.
func NewFake() *Fake { return &Fake{items: map[string]map[string]Template{}} }

func (f *Fake) List(_ context.Context, owner string) ([]Template, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Err != nil {
		return nil, f.Err
	}
	out := make([]Template, 0, len(f.items[owner]))
	for _, t := range f.items[owner] {
		out = append(out, t)
	}
	return sorted(out), nil
}

func (f *Fake) Create(_ context.Context, owner string, t Template) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Err != nil {
		return f.Err
	}
	if _, ok := f.items[owner][t.ID]; ok {
		return ErrExists
	}
	if f.items[owner] == nil {
		f.items[owner] = map[string]Template{}
	}
	f.items[owner][t.ID] = t
	return nil
}

func (f *Fake) Replace(_ context.Context, owner string, t Template) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Err != nil {
		return f.Err
	}
	if _, ok := f.items[owner][t.ID]; !ok {
		return ErrNotFound
	}
	f.items[owner][t.ID] = t
	return nil
}

func (f *Fake) Delete(_ context.Context, owner, id string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.Err != nil {
		return f.Err
	}
	if _, ok := f.items[owner][id]; !ok {
		return ErrNotFound
	}
	delete(f.items[owner], id)
	return nil
}

// sorted orders a list by name then id, so the site lists templates the same
// way on every read and two with one name are told apart the same way.
func sorted(list []Template) []Template {
	sort.Slice(list, func(i, j int) bool {
		if list[i].Name != list[j].Name {
			return list[i].Name < list[j].Name
		}
		return list[i].ID < list[j].ID
	})
	return list
}
