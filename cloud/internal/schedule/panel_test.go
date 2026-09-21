package schedule

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// Three games on one night: A 23:00, B 00:30 (overlaps A), C 03:00 (clear of
// both, touching nothing), and D the next day.
var starts = map[int64]string{
	1: "2026-10-07T23:00:00Z",
	2: "2026-10-08T00:30:00Z",
	3: "2026-10-08T03:30:00Z",
	4: "2026-10-08T23:00:00Z",
}

func TestDecodeIsStrict(t *testing.T) {
	for _, bad := range []string{
		``, `[]`, `{"games":[1],"extra":1}`, `{"games":[1]} {"games":[2]}`, `{"games":[0]}`, `{"games":[-1]}`,
		`{"games":[1,1]}`, `{"games":["1"]}`, `{"games":[1.5]}`,
		`{"resolutions":[{"sequence":[1],"keep":[1]}]}`,
		`{"resolutions":[{"sequence":[1,2],"keep":[1,1]}]}`,
		`{"resolutions":[{"sequence":[1,2],"keep":[1,2,3]}]}`,
		`{"templates":["a","b","c","d","e","f"]}`,
	} {
		if _, err := Decode([]byte(bad)); !errors.Is(err, ErrMalformed) {
			t.Errorf("%s: got %v", bad, err)
		}
	}
	ids := make([]string, MaxGames+1)
	for i := range ids {
		ids[i] = fmt.Sprint(i + 1)
	}
	if _, err := Decode([]byte(`{"games":[` + strings.Join(ids, ",") + `]}`)); !errors.Is(err, ErrMalformed) {
		t.Error("more games than the bound were accepted")
	}
	p, err := Decode([]byte(`{"games":null}`))
	if err != nil || p.Games == nil || p.Templates == nil || p.Resolutions == nil {
		t.Errorf("nulls should become empty lists: %+v %v", p, err)
	}
}

func TestStoredAndLoadRoundTripAndDamageReadsAsNothing(t *testing.T) {
	p := Panel{Games: []int64{1, 2}, Templates: []string{}, Resolutions: []Resolution{{Sequence: []int64{1, 2}, Keep: []int64{2}}}}
	if got := Load(Stored(p)); fmt.Sprint(got) != fmt.Sprint(p) {
		t.Errorf("got %+v", got)
	}
	for _, damaged := range []string{"", "{", `{"games":[0]}`, `{"games":"all"}`} {
		if got := Load(damaged); !got.IsZero() || got.Games == nil {
			t.Errorf("%q: got %+v", damaged, got)
		}
	}
}

func TestASaveWithNoConflictIsStoredSorted(t *testing.T) {
	got, err := Save(Panel{Games: []int64{4, 1, 3}}, Panel{}, starts)
	if err != nil || len(got.Unresolved) != 0 || fmt.Sprint(got.Panel.Games) != "[1 3 4]" {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestASaveThatLeavesAConflictUnansweredStoresNothing(t *testing.T) {
	got, err := Save(Panel{Games: []int64{1, 2, 4}}, Panel{}, starts)
	if err != nil || fmt.Sprint(got.Unresolved) != "[[1 2]]" {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestAnAnsweredConflictIsStoredAgainstItsSequence(t *testing.T) {
	got, err := Save(Panel{Games: []int64{1, 2, 4}, Resolutions: []Resolution{{Sequence: []int64{2, 1}, Keep: []int64{2}}}}, Panel{}, starts)
	if err != nil || len(got.Unresolved) != 0 {
		t.Fatalf("%+v %v", got, err)
	}
	if fmt.Sprint(got.Panel.Resolutions) != "[{[1 2] [2]}]" {
		t.Errorf("%+v", got.Panel.Resolutions)
	}
}

func TestAnAnswerIsCheckedNotTrusted(t *testing.T) {
	for name, r := range map[string]Resolution{
		"keeps both of two overlapping games": {Sequence: []int64{1, 2}, Keep: []int64{1, 2}},
		"keeps nothing":                       {Sequence: []int64{1, 2}, Keep: []int64{}},
	} {
		if _, err := Save(Panel{Games: []int64{1, 2}, Resolutions: []Resolution{r}}, Panel{}, starts); !errors.Is(err, ErrBadResolution) {
			t.Errorf("%s: got %v", name, err)
		}
	}
	// Two answers to one conflict: which one was meant is not ours to guess.
	twice := []Resolution{{Sequence: []int64{1, 2}, Keep: []int64{1}}, {Sequence: []int64{2, 1}, Keep: []int64{2}}}
	if _, err := Save(Panel{Games: []int64{1, 2}, Resolutions: twice}, Panel{}, starts); !errors.Is(err, ErrBadResolution) {
		t.Errorf("two answers: got %v", err)
	}
}

func TestAnAnswerToAConflictThatDoesNotExistIsDropped(t *testing.T) {
	// A game outside the sequence cannot be smuggled in through an answer:
	// the answer names a sequence that is not one, so it is simply not kept.
	got, err := Save(Panel{Games: []int64{1, 4}, Resolutions: []Resolution{{Sequence: []int64{1, 2}, Keep: []int64{2}}}}, Panel{}, starts)
	if err != nil || len(got.Panel.Resolutions) != 0 || len(got.Unresolved) != 0 {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestAnIdThisServerNeverVouchedForIsRefusedButAPastGameIsJustDropped(t *testing.T) {
	if _, err := Save(Panel{Games: []int64{1, 999}}, Panel{}, starts); !errors.Is(err, ErrUnknownGame) {
		t.Errorf("got %v", err)
	}
	// 999 was on the panel and has since left the schedule: it is over.
	got, err := Save(Panel{Games: []int64{1, 999}}, Panel{Games: []int64{999}}, starts)
	if err != nil || fmt.Sprint(got.Panel.Games) != "[1]" {
		t.Errorf("%+v %v", got, err)
	}
}

func TestResolveUsesAnAnswerOnlyForExactlyItsConflict(t *testing.T) {
	p := Panel{Resolutions: []Resolution{{Sequence: []int64{1, 2}, Keep: []int64{2}}}}
	got := Resolve(p, []int64{1, 2, 4}, nil, starts)
	if fmt.Sprint(got.Kept) != "[2 4]" || len(got.Undecided) != 0 {
		t.Errorf("answered: %+v", got)
	}
	// The NHL moves game 3 into the chain. The answer was for [1 2], not for
	// [1 2 3]: the default decides and the panel needs a decision.
	moved := map[int64]string{1: starts[1], 2: starts[2], 3: "2026-10-08T02:00:00Z", 4: starts[4]}
	got = Resolve(p, []int64{1, 2, 3, 4}, nil, moved)
	if fmt.Sprint(got.Undecided) != "[[1 2 3]]" || fmt.Sprint(got.Kept) != "[1 3 4]" {
		t.Errorf("stale: %+v", got)
	}
}

func TestResolveNeverKeepsTwoGamesThatOverlap(t *testing.T) {
	// Whatever is stored. An answer that was valid when saved and is not now
	// (game 2 moved onto game 4) must not be honoured.
	p := Panel{Resolutions: []Resolution{{Sequence: []int64{1, 2}, Keep: []int64{2}}}}
	moved := map[int64]string{1: starts[1], 2: starts[4], 4: starts[4]}
	got := Resolve(p, []int64{1, 2, 4}, nil, moved)
	for i, a := range got.Kept {
		for _, b := range got.Kept[i+1:] {
			if ta, _ := parseStart(moved[a]); true {
				if tb, _ := parseStart(moved[b]); overlaps(ta, tb) {
					t.Errorf("kept %d and %d, which overlap: %+v", a, b, got)
				}
			}
		}
	}
	if len(got.Undecided) != 1 {
		t.Errorf("%+v", got)
	}
}

func TestAGameThatHasLeftTheSeasonIsUnplacedNotKept(t *testing.T) {
	got := Resolve(Panel{}, []int64{1, 999}, nil, starts)
	if fmt.Sprint(got.Kept) != "[1]" || fmt.Sprint(got.Unplaced) != "[999]" {
		t.Errorf("%+v", got)
	}
}
