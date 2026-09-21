package season

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func file(rows ...string) []byte {
	return []byte(`{"generatedAt":"x","teams":{"MTL":"Montréal Canadiens","TOR":"Toronto Maple Leafs"},"games":[` + strings.Join(rows, ",") + `]}`)
}

const good = `{"id":2026020001,"date":"2026-10-07","start":"2026-10-07T23:00:00Z","away":"MTL","home":"TOR","type":2,"venue":"Scotiabank Arena"}`

func TestAGoodRowIsRebuiltFieldByField(t *testing.T) {
	s, dropped, err := Parse(file(good))
	if err != nil || dropped != 0 || len(s.Games) != 1 {
		t.Fatalf("%v %d %+v", err, dropped, s.Games)
	}
	want := Game{GameID: 2026020001, Date: "2026-10-07", Start: "2026-10-07T23:00:00Z", Away: "MTL", Home: "TOR", Venue: "Scotiabank Arena", Type: 2}
	if s.Games[0] != want {
		t.Errorf("got %+v", s.Games[0])
	}
	if g, ok := s.Find(2026020001); !ok || g != want {
		t.Errorf("Find: %+v %v", g, ok)
	}
	out, _ := json.Marshal(s.Games[0])
	if strings.Contains(string(out), "generatedAt") {
		t.Errorf("a field nobody checked was passed through: %s", out)
	}
}

func TestARowThatFailsACheckIsDroppedAndCounted(t *testing.T) {
	row := func(change string) string {
		m := map[string]any{}
		_ = json.Unmarshal([]byte(good), &m)
		var patch map[string]any
		if err := json.Unmarshal([]byte(change), &patch); err != nil {
			t.Fatal(err)
		}
		for k, v := range patch {
			m[k] = v
		}
		b, _ := json.Marshal(m)
		return string(b)
	}
	for _, change := range []string{
		`{"id":0}`, `{"id":-4}`, `{"start":"soon"}`, `{"start":"2026-10-07T23:00:00"}`, `{"start":""}`,
		`{"away":"mtl"}`, `{"away":"<b>"}`, `{"home":"TORONTO"}`, `{"home":"T"}`, `{"home":"MTL"}`,
		`{"away":"M\u0000L"}`, `{"type":0}`, `{"type":9}`,
		`{"date":""}`, `{"date":"2026-13-01"}`, `{"date":"2026-02-30"}`, `{"date":"10/07/2026"}`, `{"date":"2026-10-07T00:00:00Z"}`, `{"date":"<b>2026-1"}`,
	} {
		s, dropped, err := Parse(file(row(change), good))
		if err != nil {
			t.Fatalf("%s: %v", change, err)
		}
		if dropped != 1 || len(s.Games) != 1 || s.Games[0].GameID != 2026020001 {
			t.Errorf("%s: dropped %d, kept %+v", change, dropped, s.Games)
		}
	}
}

func TestTheSameIdTwiceIsOneGame(t *testing.T) {
	s, dropped, _ := Parse(file(good, good))
	if len(s.Games) != 1 || dropped != 1 {
		t.Errorf("%d games, %d dropped", len(s.Games), dropped)
	}
}

func TestStartsAreWrittenInOneSpellingAndGamesAreInOrder(t *testing.T) {
	later := strings.Replace(strings.Replace(good, "2026020001", "2026020009", 1), "2026-10-07T23:00:00Z", "2026-10-07T19:30:00-04:00", 1)
	sameTime := strings.Replace(good, "2026020001", "2026020000", 1)
	s, _, _ := Parse(file(later, good, sameTime))
	var ids []int64
	for _, g := range s.Games {
		ids = append(ids, g.GameID)
	}
	if fmt.Sprint(ids) != "[2026020000 2026020001 2026020009]" || s.Games[2].Start != "2026-10-07T23:30:00Z" {
		t.Errorf("%v %+v", ids, s.Games[2])
	}
}

func TestAVenueIsOneShortPrintableLine(t *testing.T) {
	long := strings.Repeat("Arena ", 30)
	s, _, _ := Parse(file(strings.Replace(good, "Scotiabank Arena", `Bell\n\tCentre\u0007 `+long, 1)))
	v := s.Games[0].Venue
	if strings.ContainsAny(v, "\n\t\a") || !strings.HasPrefix(v, "Bell Centre Arena") || len([]rune(v)) > 60 {
		t.Errorf("%q", v)
	}
}

func TestAFileWithTooManyRowsIsRefusedWhole(t *testing.T) {
	rows := make([]string, MaxGames+1)
	for i := range rows {
		rows[i] = strings.Replace(good, "2026020001", fmt.Sprint(2026020001+i), 1)
	}
	if _, _, err := Parse(file(rows...)); !errors.Is(err, ErrTooLarge) {
		t.Errorf("got %v", err)
	}
	if _, _, err := Parse([]byte("<html>")); err == nil {
		t.Error("not JSON must be an error")
	}
}

func TestStartsLeavesOutWhatIsNotInTheSeason(t *testing.T) {
	s, _, _ := Parse(file(good))
	got := s.Starts([]int64{2026020001, 5})
	if len(got) != 1 || got[2026020001] != "2026-10-07T23:00:00Z" {
		t.Errorf("%v", got)
	}
}

type source struct {
	body  []byte
	err   error
	calls int
}

func (s *source) fetch(context.Context) ([]byte, error) { s.calls++; return s.body, s.err }

func cache(src *source, clock *time.Time) *Cache {
	return &Cache{Fetch: src.fetch, Now: func() time.Time { return *clock }, FreshFor: 10 * time.Minute, StaleFor: 6 * time.Hour}
}

func TestTheCacheFetchesOnceWhileFresh(t *testing.T) {
	src, clock := &source{body: file(good)}, time.Unix(1e9, 0)
	c := cache(src, &clock)
	for i := 0; i < 5; i++ {
		if s, err := c.Get(context.Background()); err != nil || len(s.Games) != 1 {
			t.Fatal(err)
		}
		clock = clock.Add(time.Minute)
	}
	if src.calls != 1 {
		t.Errorf("fetched %d times", src.calls)
	}
	clock = clock.Add(10 * time.Minute)
	_, _ = c.Get(context.Background())
	if src.calls != 2 {
		t.Errorf("not refreshed when due: %d", src.calls)
	}
}

func TestABadFetchDoesNotReplaceAGoodSeasonUntilItIsTooOldToStandBehind(t *testing.T) {
	src, clock := &source{body: file(good)}, time.Unix(1e9, 0)
	c := cache(src, &clock)
	_, _ = c.Get(context.Background())
	for name, breakIt := range map[string]func(){
		"the fetch fails":    func() { src.body, src.err = nil, errors.New("down") },
		"it is not JSON":     func() { src.body, src.err = []byte("<html>"), nil },
		"it has no games":    func() { src.body, src.err = file(), nil },
		"every row is wrong": func() { src.body, src.err = file(strings.Replace(good, "MTL", "mtl", 1)), nil },
	} {
		breakIt()
		clock = clock.Add(11 * time.Minute)
		if s, err := c.Get(context.Background()); err != nil || len(s.Games) != 1 {
			t.Errorf("%s: %v %d", name, err, len(s.Games))
		}
	}
	clock = clock.Add(6 * time.Hour)
	if _, err := c.Get(context.Background()); err == nil {
		t.Error("a season older than StaleFor was served with nothing behind it")
	}
}

func TestWithNothingCachedAFailureIsAnError(t *testing.T) {
	src, clock := &source{err: errors.New("down")}, time.Unix(1e9, 0)
	if _, err := cache(src, &clock).Get(context.Background()); err == nil {
		t.Error("want an error")
	}
}

func TestDroppedRowsAreReported(t *testing.T) {
	src, clock := &source{body: file(good, strings.Replace(good, "MTL", "mtl", 1))}, time.Unix(1e9, 0)
	c := cache(src, &clock)
	n := 0
	c.Dropped = func(d int) { n = d }
	_, _ = c.Get(context.Background())
	if n != 1 {
		t.Errorf("reported %d", n)
	}
}

func TestClubNamesAreCheckedLikeEverythingElse(t *testing.T) {
	body := []byte(`{"teams":{"MTL":"Montréal Canadiens","TOR":"  Toronto\n Maple\tLeafs ","bos":"Boston Bruins","NYR":"","SEA":"` +
		strings.Repeat("Kraken ", 20) + `","<b>":"x"},"games":[` + good + `]}`)
	s, dropped, err := Parse(body)
	if err != nil {
		t.Fatal(err)
	}
	if s.Teams["MTL"] != "Montréal Canadiens" || s.Teams["TOR"] != "Toronto Maple Leafs" {
		t.Errorf("%v", s.Teams)
	}
	if _, ok := s.Teams["bos"]; ok || len(s.Teams) != 3 || dropped != 3 {
		t.Errorf("kept %v, dropped %d", s.Teams, dropped)
	}
	if n := len([]rune(s.Teams["SEA"])); n > 40 {
		t.Errorf("a name of %d characters", n)
	}
	// A game whose clubs have no names still lists.
	s, _, _ = Parse([]byte(`{"games":[` + good + `]}`))
	if len(s.Games) != 1 || s.Teams == nil {
		t.Errorf("%+v", s)
	}
}
