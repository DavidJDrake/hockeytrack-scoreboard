package director

import (
	"math/rand"
	"testing"
	"time"

	"hockeytrack-scoreboard/internal/schedule"
)

var clock = time.Date(2026, 10, 14, 23, 30, 0, 0, time.UTC) // 7:30 PM Eastern

const hold = 3 * time.Hour

// A night with two kept games: 7 PM and 10 PM Eastern, plus one tomorrow.
const (
	early    int64 = 2026020101
	late     int64 = 2026020102
	tomorrow int64 = 2026020103
	stranger int64 = 2026029999 // a game not on this panel
)

func night() Panel {
	return Panel{
		Kept: []int64{early, late, tomorrow},
		Games: map[int64]Game{
			early:    {Start: clock.Add(-30 * time.Minute), State: "PRE"},
			late:     {Start: clock.Add(150 * time.Minute), State: "PRE"},
			tomorrow: {Start: clock.Add(24 * time.Hour)},
		},
		Hold: hold,
	}
}

func TestALiveKeptGameIsCurrentEvenPastItsSlot(t *testing.T) {
	p := night()
	p.Games[early] = Game{Start: clock.Add(-4 * time.Hour), State: "LIVE"} // triple overtime
	if got := Current(p, clock); got != early {
		t.Errorf("got %d, want the live game %d", got, early)
	}
	// Two kept games live at once, a delayed start running into the next:
	// the one already showing stays; otherwise the earliest.
	p.Games[late] = Game{Start: clock.Add(-10 * time.Minute), State: "LIVE"}
	p.Showing, p.Sent = late, late
	if got := Current(p, clock); got != late {
		t.Errorf("got %d, want the live game already showing %d", got, late)
	}
	p.Showing, p.Sent = 0, 0
	if got := Current(p, clock); got != early {
		t.Errorf("got %d, want the earlier live game %d", got, early)
	}
}

func TestAFinalHoldsUntilItsHoldEnds(t *testing.T) {
	p := night()
	p.Kept = []int64{early, tomorrow}
	ended := clock.Add(-40 * time.Minute)
	p.Games[early] = Game{Start: clock.Add(-3 * time.Hour), State: "FINAL", FinalAt: ended}
	if got := Current(p, clock); got != early {
		t.Errorf("inside the hold: got %d, want the final %d", got, early)
	}
	if got := Current(p, ended.Add(hold-time.Second)); got != early {
		t.Errorf("a second before the hold ends: got %d, want %d", got, early)
	}
	if got := Current(p, ended.Add(hold)); got != tomorrow {
		t.Errorf("hold over: got %d, want the next game %d", got, tomorrow)
	}
}

func TestTheNextPuckDropCutsAFinalShort(t *testing.T) {
	// Two games in one day (design section 6, decision 7): the early final is
	// held, but only until the late game's puck drop, hold or no hold.
	p := night()
	p.Games[early] = Game{Start: clock.Add(-3 * time.Hour), State: "FINAL", FinalAt: clock.Add(-10 * time.Minute)}
	if got := Current(p, clock); got != early {
		t.Errorf("before the late puck drop: got %d, want the final %d", got, early)
	}
	drop := p.Games[late].Start
	if got := Current(p, drop.Add(-time.Second)); got != early {
		t.Errorf("a second before puck drop: got %d, want %d", got, early)
	}
	if got := Current(p, drop); got != late {
		t.Errorf("at puck drop: got %d, want the late game %d", got, late)
	}
	// The reducer catching up and marking it live changes nothing.
	p.Games[late] = Game{Start: drop, State: "LIVE"}
	if got := Current(p, drop.Add(time.Minute)); got != late {
		t.Errorf("live: got %d, want %d", got, late)
	}
}

func TestTheMostRecentFinalIsTheOneHeld(t *testing.T) {
	p := night()
	p.Kept = []int64{early, late}
	p.Games[early] = Game{Start: clock.Add(-6 * time.Hour), State: "FINAL", FinalAt: clock.Add(-3*time.Hour - time.Minute)}
	p.Games[late] = Game{Start: clock.Add(-3 * time.Hour), State: "FINAL", FinalAt: clock.Add(-20 * time.Minute)}
	if got := Current(p, clock); got != late {
		t.Errorf("got %d, want the later final %d", got, late)
	}
}

func TestTheNextKeptGameIsCurrentWhenNothingIsOn(t *testing.T) {
	p := night()
	if got := Current(p, clock); got != early {
		t.Errorf("got %d, want the next game %d", got, early)
	}
	// A game the reducer never saw is upcoming until its slot has passed,
	// and over after: the panel is not parked on it for the season.
	delete(p.Games, early)
	p.Games[early] = Game{Start: clock.Add(-30 * time.Minute)}
	if got := Current(p, clock); got != early {
		t.Errorf("unseen, in its slot: got %d, want %d", got, early)
	}
	if got := Current(p, clock.Add(schedule.Occupies)); got != late {
		t.Errorf("unseen, slot passed: got %d, want %d", got, late)
	}
}

func TestNothingAheadSendsNothing(t *testing.T) {
	p := Panel{Kept: []int64{early}, Games: map[int64]Game{
		early: {Start: clock.Add(-30 * time.Hour), State: "FINAL", FinalAt: clock.Add(-28 * time.Hour)},
	}, Hold: hold, Showing: early, Sent: early}
	if got := Current(p, clock); got != 0 {
		t.Errorf("got %d, want 0", got)
	}
	if got := Current(Panel{}, clock); got != 0 {
		t.Errorf("empty panel: got %d, want 0", got)
	}
}

func TestAnAbsentGameIsOverNotAnError(t *testing.T) {
	// A kept id with nothing known about it -- gone from the season, never
	// in the games table -- is skipped, and the panel moves on.
	p := night()
	p.Kept = []int64{stranger, late}
	delete(p.Games, stranger)
	if got := Current(p, clock); got != late {
		t.Errorf("got %d, want %d", got, late)
	}
	// The same for an override the owner chose that has since vanished.
	p.Showing, p.Sent = stranger, 0
	if got := Current(p, clock); got != late {
		t.Errorf("vanished override: got %d, want %d", got, late)
	}
}

func TestTheOwnersOverrideWinsUntilThatGameIsOver(t *testing.T) {
	// The owner pressed "Show on panel" for a game not on the schedule
	// (Showing differs from what the director last sent). It stands.
	p := night()
	p.Games[stranger] = Game{Start: clock.Add(-time.Hour), State: "LIVE"}
	p.Showing, p.Sent = stranger, early
	if got := Current(p, clock); got != stranger {
		t.Errorf("live override: got %d, want %d", got, stranger)
	}
	// Its final is held like any other: "Show on panel" is how a final is
	// brought back (section 12), and it is not taken down at the horn.
	p.Games[stranger] = Game{Start: clock.Add(-3 * time.Hour), State: "FINAL", FinalAt: clock.Add(-time.Hour)}
	if got := Current(p, clock); got != stranger {
		t.Errorf("final override inside its hold: got %d, want %d", got, stranger)
	}
	// Over, and the schedule resumes.
	if got := Current(p, clock.Add(hold)); got != late {
		t.Errorf("override over: got %d, want %d", got, late)
	}
	// Pressing the game the director sent anyway is not an override.
	p.Showing, p.Sent = early, early
	if got := Current(p, clock); got != early {
		t.Errorf("got %d, want %d", got, early)
	}
	// Nor is a panel with no game: 0 is never an override.
	p.Showing, p.Sent = 0, early
	if got := Current(p, clock); got != early {
		t.Errorf("cleared panel: got %d, want %d", got, early)
	}
}

// The strip's "up next" (SCO-56), against the same night.
func TestTheNextGameIsTheFirstKeptGameAfterTheCurrentOne(t *testing.T) {
	// 7:30 PM, the early game current and upcoming: the late game is next.
	p := night()
	if got := Next(p, clock); got != late {
		t.Errorf("early upcoming: got %d, want %d", got, late)
	}
	// Live, then final and held: still the late game, and never itself.
	p.Games[early] = Game{Start: clock.Add(-30 * time.Minute), State: "LIVE"}
	if got := Next(p, clock); got != late {
		t.Errorf("early live: got %d, want %d", got, late)
	}
	p.Games[early] = Game{Start: clock.Add(-3 * time.Hour), State: "FINAL", FinalAt: clock.Add(-10 * time.Minute)}
	if got := Next(p, clock); got != late {
		t.Errorf("early final and held: got %d, want %d", got, late)
	}
	// The late game current: tomorrow's is next, even though the early
	// final is still inside its hold -- it is behind, not ahead.
	p.Games[early] = Game{Start: clock.Add(-30 * time.Minute), State: "FINAL", FinalAt: clock.Add(2 * time.Hour)}
	at := p.Games[late].Start.Add(time.Minute)
	if got := Next(p, at); got != tomorrow {
		t.Errorf("late current: got %d, want %d", got, tomorrow)
	}
}

func TestNoNextGameWhenNothingIsAhead(t *testing.T) {
	// One kept game, current: nothing after it.
	p := night()
	p.Kept = []int64{early}
	if got := Next(p, clock); got != 0 {
		t.Errorf("one game: got %d, want 0", got)
	}
	// The last kept game current: nothing after it either.
	p = night()
	if got := Next(p, clock.Add(24*time.Hour+time.Minute)); got != 0 {
		t.Errorf("last game current: got %d, want 0", got)
	}
	if got := Next(Panel{}, clock); got != 0 {
		t.Errorf("empty panel: got %d, want 0", got)
	}
}

// Nothing current means nothing ahead: a kept game still to come is itself
// the current game (Current's rule 4, the panel counts down to it), so the
// only way Current says 0 is a schedule with nothing left in it. The strip
// then has nothing to name either -- not last night's final, not the game
// that passed unseen. Next's "after now" reading is what makes that so.
func TestNothingCurrentMeansNothingNext(t *testing.T) {
	p := Panel{Kept: []int64{early, late}, Games: map[int64]Game{
		early: {Start: clock.Add(-30 * time.Hour), State: "FINAL", FinalAt: clock.Add(-28 * time.Hour)},
		late:  {Start: clock.Add(-4 * time.Hour)},
	}, Hold: hold, Showing: early, Sent: early}
	if got := Current(p, clock); got != 0 {
		t.Fatalf("the case wants nothing current, got %d", got)
	}
	if got := Next(p, clock); got != 0 {
		t.Errorf("got %d, want 0", got)
	}
	// And the moment a game is added ahead, it is current, and next is the
	// one after it, not it.
	p.Kept = []int64{early, late, tomorrow}
	p.Games[tomorrow] = Game{Start: clock.Add(24 * time.Hour)}
	if got := Current(p, clock); got != tomorrow {
		t.Fatalf("got %d current, want %d", got, tomorrow)
	}
	if got := Next(p, clock); got != 0 {
		t.Errorf("got %d next, want 0", got)
	}
}

// A kept game that has already started is never "next", whatever the current
// game is doing: the strip would be naming a puck drop that has passed. The
// early game in triple overtime is current; the late game's start went by
// while it played, so tomorrow's game is what is next.
func TestANextThatStartsBeforeNowIsSkipped(t *testing.T) {
	p := night()
	p.Games[early] = Game{Start: clock.Add(-4 * time.Hour), State: "LIVE"}
	p.Games[late] = Game{Start: clock.Add(-10 * time.Minute)}
	if got := Current(p, clock); got != early {
		t.Fatalf("the case wants the live game current, got %d", got)
	}
	if got := Next(p, clock); got != tomorrow {
		t.Errorf("got %d, want %d", got, tomorrow)
	}
	// The same with the late game already live: two live games at once.
	p.Games[late] = Game{Start: clock.Add(-10 * time.Minute), State: "LIVE"}
	if got := Next(p, clock); got != tomorrow {
		t.Errorf("two live: got %d, want %d", got, tomorrow)
	}
}

// Two kept games exactly schedule.Occupies apart: the schedule allows the
// pair (touching exactly is not overlapping), so the second is next while the
// first is current, upcoming or live, not the game after it.
func TestAGameStartingExactlyAtTheCurrentOnesEndIsNext(t *testing.T) {
	p := night()
	touching := p.Games[early].Start.Add(schedule.Occupies)
	p.Games[late] = Game{Start: touching, State: "PRE"}
	if got := Next(p, clock); got != late {
		t.Errorf("early upcoming: got %d, want %d", got, late)
	}
	p.Games[early] = Game{Start: clock.Add(-30 * time.Minute), State: "LIVE"}
	if got := Next(p, clock); got != late {
		t.Errorf("early live: got %d, want %d", got, late)
	}
	// And a final that ends on the second's puck drop exactly.
	p.Games[early] = Game{Start: clock.Add(-30 * time.Minute), State: "FINAL", FinalAt: touching}
	if got := Next(p, clock); got != late {
		t.Errorf("early final: got %d, want %d", got, late)
	}
}

// The owner's override is not kept and is not skipped over blindly: the next
// game is the first kept one after the override ends.
func TestTheNextGameAfterAnOverrideIsTheFirstKeptGameAfterIt(t *testing.T) {
	p := night()
	p.Games[stranger] = Game{Start: clock.Add(-time.Hour), State: "LIVE"}
	p.Showing, p.Sent = stranger, early
	if got := Current(p, clock); got != stranger {
		t.Fatalf("the case wants the override current, got %d", got)
	}
	// Its slot ends at 9:10 PM; the late game starts at 10 PM and is next.
	if got := Next(p, clock); got != late {
		t.Errorf("got %d, want %d", got, late)
	}
}

// The property the security review asks for: whatever the inputs, the rule
// names a kept game, the game the owner put there, or nothing. Random panels
// rather than chosen ones, so the guarantee does not rest on the cases above
// having thought of everything.
func TestTheRuleNeverNamesAGameOutsideTheKeptSet(t *testing.T) {
	rng := rand.New(rand.NewSource(42))
	states := []string{"", "PRE", "LIVE", "FINAL"}
	for i := 0; i < 5000; i++ {
		p := Panel{Games: map[int64]Game{}, Hold: time.Duration(rng.Intn(300)) * time.Minute}
		n := rng.Intn(6)
		for j := 0; j < n; j++ {
			p.Kept = append(p.Kept, int64(1000+j))
		}
		for id := int64(1000); id < 1010; id++ {
			if rng.Intn(4) == 0 {
				continue // absent
			}
			offset := time.Duration(rng.Intn(48*60)-24*60) * time.Minute
			g := Game{Start: clock.Add(offset), State: states[rng.Intn(len(states))]}
			if g.State == "FINAL" {
				g.FinalAt = g.Start.Add(time.Duration(rng.Intn(200)) * time.Minute)
			}
			p.Games[id] = g
		}
		p.Showing, p.Sent = int64(rng.Intn(11))+999, int64(rng.Intn(11))+999
		if p.Showing == 999 {
			p.Showing = 0
		}
		at := clock.Add(time.Duration(rng.Intn(600)-300) * time.Minute)
		got := Current(p, at)
		if got != 0 && got != p.Showing && !contains(p.Kept, got) {
			t.Fatalf("case %d: %d is neither kept %v, showing %d nor 0", i, got, p.Kept, p.Showing)
		}
		// The next game is held to more: kept or nothing, never the game
		// being sent, and always still ahead.
		if next := Next(p, at); next != 0 {
			if !contains(p.Kept, next) || next == got || !p.Games[next].Start.After(at) {
				t.Fatalf("case %d: next %d with current %d, kept %v, start %v at %v", i, next, got, p.Kept, p.Games[next].Start, at)
			}
		}
	}
}
