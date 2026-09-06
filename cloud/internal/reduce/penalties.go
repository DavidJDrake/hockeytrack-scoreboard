package reduce

import (
	"sort"
	"strconv"
	"strings"
)

// parseClock turns "06:58" into 418 seconds.
func parseClock(mmss string) int {
	parts := strings.SplitN(mmss, ":", 2)
	if len(parts) != 2 {
		return 0
	}
	m, _ := strconv.Atoi(parts[0])
	sec, _ := strconv.Atoi(parts[1])
	return m*60 + sec
}

// nowT is the absolute game time of the latest clock heartbeat.
func (s *State) nowT() int {
	elapsed := PeriodLength(s.Period.Number, s.OTLen) - s.Clock.Seconds
	if elapsed < 0 {
		elapsed = 0
	}
	return GameTime(s.Period.Number, elapsed, s.OTLen)
}

// addPenalty boxes a penalty play. Penalty shots and unknown codes are
// not boxed; misconducts are shown but never end on a goal.
func (s *State) addPenalty(d playDetail) {
	det := d.Raw.Details
	var endsOnGoal bool
	switch det.TypeCode {
	case "MIN", "BEN":
		endsOnGoal = true
	case "MAJ", "MAT", "MIS":
		endsOnGoal = false
	default:
		return
	}
	if det.Duration <= 0 {
		return
	}
	pid := det.CommittedByPlayerID
	if det.TypeCode == "BEN" || pid == 0 {
		pid = det.ServedByPlayerID
	}
	period := d.Raw.PeriodDescriptor.Number
	if period == 0 {
		period = d.Period
	}
	if period > s.Period.Number { // a play from a period we have not seen a heartbeat for yet
		s.Period = Period{Number: period, Type: d.Raw.PeriodDescriptor.PeriodType, Label: PeriodLabel(period, d.Raw.PeriodDescriptor.PeriodType)}
	}
	start := GameTime(period, parseClock(d.TimeInPeriod), s.OTLen)
	s.Penalties = append(s.Penalties, Penalty{
		Team: d.ActingTeam, Number: s.Roster[pid], Type: det.TypeCode,
		Seconds: det.Duration * 60, Duration: det.Duration * 60, EndsOnGoal: endsOnGoal, StartT: start,
	})
	sort.SliceStable(s.Penalties, func(i, j int) bool { return s.Penalties[i].StartT < s.Penalties[j].StartT })
}

// tickPenalties recomputes remaining time from the current game clock and
// drops anything that has expired.
func (s *State) tickPenalties() {
	now := s.nowT()
	kept := s.Penalties[:0]
	for _, p := range s.Penalties {
		remaining := p.Duration - (now - p.StartT)
		if remaining > p.Duration {
			remaining = p.Duration // clock ran backwards (correction); never exceed the sentence
		}
		if remaining <= 0 {
			continue
		}
		p.Seconds = remaining
		kept = append(kept, p)
	}
	s.Penalties = kept
}

// endMinorOnPowerPlayGoal releases the shorthanded side's oldest minor when
// the team on the power play scores. The situation code decides who was on
// the power play; if it says nobody, nothing is released.
func (s *State) endMinorOnPowerPlayGoal(scoringTeam string) {
	if s.Situation.PP != scoringTeam {
		return
	}
	for i, p := range s.Penalties {
		if p.Team != scoringTeam && p.EndsOnGoal {
			s.Penalties = append(s.Penalties[:i], s.Penalties[i+1:]...)
			return
		}
	}
}
