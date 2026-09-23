package devices

import (
	"hockeytrack-scoreboard/internal/schedule"
	"hockeytrack-scoreboard/internal/settings"

	"context"
	"errors"
	"fmt"
	"reflect"
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

func TestClaimBindsAnUnclaimedDeviceExactlyOnce(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	if err := st.Register(ctx, "scoreboard-01"); err != nil {
		t.Fatal(err)
	}
	if err := st.Claim(ctx, "scoreboard-01", "user-a"); err != nil {
		t.Fatalf("first claim: %v", err)
	}
	if err := st.Claim(ctx, "scoreboard-01", "user-b"); !errors.Is(err, ErrAlreadyClaimed) {
		t.Errorf("second claim err = %v, want ErrAlreadyClaimed", err)
	}
	d, found, _ := st.Get(ctx, "scoreboard-01")
	if !found || d.Owner != "user-a" {
		t.Fatalf("after claims: found=%v owner=%q, want true/user-a", found, d.Owner)
	}
}

func TestListByOwnerReturnsOnlyThatOwnersDevices(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	for _, tc := range []struct{ thing, owner string }{
		{"scoreboard-01", "user-a"},
		{"scoreboard-02", "user-b"},
		{"scoreboard-03", "user-a"},
	} {
		if err := st.Register(ctx, tc.thing); err != nil {
			t.Fatal(err)
		}
		if err := st.Claim(ctx, tc.thing, tc.owner); err != nil {
			t.Fatal(err)
		}
	}
	got, err := st.ListByOwner(ctx, "user-a")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("ListByOwner returned %d devices, want 2", len(got))
	}
	for _, d := range got {
		if d.Owner != "user-a" {
			t.Errorf("got a device owned by %q", d.Owner)
		}
	}
}

func TestUnbindRequiresTheCurrentOwnerAndLeavesTheDeviceClaimable(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01")
	_ = st.Claim(ctx, "scoreboard-01", "user-a")
	if err := st.Unbind(ctx, "scoreboard-01", "user-b"); !errors.Is(err, ErrNotOwner) {
		t.Errorf("unbind by a stranger err = %v, want ErrNotOwner", err)
	}
	if err := st.Unbind(ctx, "scoreboard-01", "user-a"); err != nil {
		t.Fatalf("unbind by owner: %v", err)
	}
	if err := st.Claim(ctx, "scoreboard-01", "user-b"); err != nil {
		t.Errorf("claim after unbind: %v", err)
	}
}

func TestUpdateEnforcesOwnershipAndImmutability(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01")
	_ = st.Claim(ctx, "scoreboard-01", "user-a")

	// Non-owner cannot update.
	if err := st.Update(ctx, Device{ThingName: "scoreboard-01", Owner: "user-b", Name: "New Name"}); !errors.Is(err, ErrNotOwner) {
		t.Errorf("update by non-owner err = %v, want ErrNotOwner", err)
	}

	// Owner can update Name and GameID.
	if err := st.Update(ctx, Device{ThingName: "scoreboard-01", Owner: "user-a", Name: "Living Room", GameID: 2025020123}); err != nil {
		t.Fatalf("update by owner: %v", err)
	}
	d, _, _ := st.Get(ctx, "scoreboard-01")
	if d.Name != "Living Room" || d.GameID != 2025020123 {
		t.Errorf("after update: Name=%q GameID=%d, want Living Room/2025020123", d.Name, d.GameID)
	}

	// ThingName must remain immutable: attempting to change it via Update has
	// no path to do so (Update keys on the original ThingName), and the row
	// stored under the original name is unaffected.
	if err := st.Update(ctx, Device{ThingName: "scoreboard-01", Owner: "user-a", Name: "Still Living Room"}); err != nil {
		t.Fatalf("update again: %v", err)
	}
	d, _, _ = st.Get(ctx, "scoreboard-01")
	if d.ThingName != "scoreboard-01" {
		t.Errorf("ThingName was mutated to %q, want scoreboard-01 (immutable)", d.ThingName)
	}
}

func TestItemRoundTripsThroughDynamoAttributes(t *testing.T) {
	in := Device{ThingName: "scoreboard-7qf2", Owner: "sub-123", Name: "Living room", GameID: 2026020001}
	item, err := marshalDevice(in)
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"thingName", "owner", "name", "gameId"} {
		if _, ok := item[key]; !ok {
			t.Errorf("marshalled item is missing %q", key)
		}
	}
	out, err := unmarshalDevice(item)
	if err != nil {
		t.Fatal(err)
	}
	in.Schedule = schedule.Load("") // nothing asked for reads back as empty lists
	if !reflect.DeepEqual(out, in) {
		t.Errorf("round trip = %+v, want %+v", out, in)
	}
}

func TestAnUnclaimedDeviceMarshalsWithoutAnOwnerAttribute(t *testing.T) {
	item, err := marshalDevice(Device{ThingName: "scoreboard-7qf2"})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := item["owner"]; ok {
		t.Error("unclaimed device wrote an owner attribute; the claim condition depends on its absence")
	}
}

func TestRegisterRejectsInvalidThingNames(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	for _, name := range []string{"", "has space", "slash/here", "semi;colon", "colon:not-allowed", "dot.not-allowed", "hash#wild", "plus+wild"} {
		if err := st.Register(ctx, name); !errors.Is(err, ErrInvalidThingName) {
			t.Errorf("Register(%q) err = %v, want ErrInvalidThingName", name, err)
		}
	}
	if _, found, _ := st.Get(ctx, ""); found {
		t.Error("an invalid thing name must not be stored")
	}
	for _, name := range []string{"scoreboard-01", "scoreboard_01", "ABC123"} {
		if err := st.Register(ctx, name); err != nil {
			t.Errorf("Register(%q) with a valid name: %v", name, err)
		}
	}
}

func TestCondFailureDistinguishesMissingFromPresentByTheReturnedItem(t *testing.T) {
	if err := condFailure(nil, ErrNotFound, ErrAlreadyClaimed); !errors.Is(err, ErrNotFound) {
		t.Errorf("nil item err = %v, want ErrNotFound", err)
	}
	item := map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: "scoreboard-01"}}
	if err := condFailure(item, ErrNotFound, ErrAlreadyClaimed); !errors.Is(err, ErrAlreadyClaimed) {
		t.Errorf("present item err = %v, want ErrAlreadyClaimed", err)
	}
}

func TestWhenAGameWasChosenRoundTripsAndOldRowsReadAsNever(t *testing.T) {
	item, err := marshalDevice(Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", GameID: 5, ChosenAt: 1789871240471})
	if err != nil {
		t.Fatal(err)
	}
	back, err := unmarshalDevice(item)
	if err != nil || back.ChosenAt != 1789871240471 {
		t.Fatalf("round trip: %+v, %v", back, err)
	}
	// Every row written before this field existed.
	delete(item, "chosenAt")
	old, err := unmarshalDevice(item)
	if err != nil || old.ChosenAt != 0 {
		t.Fatalf("a row with no chosenAt: %+v, %v", old, err)
	}
}

func TestAPanelsOwnSettingsRoundTripAndOldRowsHaveNone(t *testing.T) {
	lead := 120
	in := Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", Display: settings.Settings{CountdownLeadMin: &lead}}
	item, err := marshalDevice(in)
	if err != nil {
		t.Fatal(err)
	}
	back, err := unmarshalDevice(item)
	if err != nil || back.Display.CountdownLeadMin == nil || *back.Display.CountdownLeadMin != 120 {
		t.Fatalf("round trip: %+v, %v", back.Display, err)
	}
	delete(item, "display")
	old, err := unmarshalDevice(item)
	if err != nil || old.Display != (settings.Settings{}) {
		t.Fatalf("a row with no display: %+v, %v", old.Display, err)
	}
	// A damaged attribute must not take the panel off its owner's list.
	item["display"] = &types.AttributeValueMemberS{Value: "{not json"}
	if got, err := unmarshalDevice(item); err != nil || got.Display != (settings.Settings{}) {
		t.Fatalf("a damaged display: %+v, %v", got.Display, err)
	}
}

func TestReleasingAPanelClearsItsSettings(t *testing.T) {
	f := NewFake()
	ctx := context.Background()
	_ = f.Register(ctx, "scoreboard-7qf2")
	_ = f.Claim(ctx, "scoreboard-7qf2", "sub-a")
	hold := 30
	_ = f.Update(ctx, Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", Display: settings.Settings{FinalHoldMin: &hold}})
	_ = f.Unbind(ctx, "scoreboard-7qf2", "sub-a")
	d, _, _ := f.Get(ctx, "scoreboard-7qf2")
	if d.Display != (settings.Settings{}) {
		t.Errorf("the next owner inherits %+v", d.Display)
	}
}

func TestAScheduleSurvivesTheRoundTripAndDamageReadsAsNothing(t *testing.T) {
	want := schedule.Panel{Games: []int64{2026020001, 2026020002}, Templates: []string{},
		Resolutions: []schedule.Resolution{{Sequence: []int64{2026020001, 2026020002}, Keep: []int64{2026020002}}}}
	item, err := marshalDevice(Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", Schedule: want})
	if err != nil {
		t.Fatal(err)
	}
	got, err := unmarshalDevice(item)
	if err != nil || fmt.Sprint(got.Schedule) != fmt.Sprint(want) {
		t.Fatalf("%+v %v", got.Schedule, err)
	}
	delete(item, "schedule") // a row from before schedules existed
	if got, err := unmarshalDevice(item); err != nil || !got.Schedule.IsZero() || got.Schedule.Games == nil {
		t.Fatalf("no attribute: %+v %v", got.Schedule, err)
	}
	item["schedule"] = &types.AttributeValueMemberS{Value: `{"games":[-1]}`}
	if got, err := unmarshalDevice(item); err != nil || !got.Schedule.IsZero() {
		t.Fatalf("damaged: %+v %v", got.Schedule, err)
	}
}

func TestReleasingAPanelClearsItsSchedule(t *testing.T) {
	// Which games somebody chose to watch is theirs. The next owner of the
	// panel does not get to read it.
	f := NewFake()
	ctx := context.Background()
	_ = f.Register(ctx, "scoreboard-7qf2")
	_ = f.Claim(ctx, "scoreboard-7qf2", "sub-a")
	_ = f.Update(ctx, Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", Schedule: schedule.Panel{Games: []int64{2026020001}}})
	_ = f.Unbind(ctx, "scoreboard-7qf2", "sub-a")
	if d, _, _ := f.Get(ctx, "scoreboard-7qf2"); !d.Schedule.IsZero() {
		t.Errorf("the next owner inherits %+v", d.Schedule)
	}
}

func TestTheDirectorsMarkerRoundTripsAndOldRowsReadAsNeverSent(t *testing.T) {
	item, err := marshalDevice(Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", GameID: 5, Sent: 5})
	if err != nil {
		t.Fatal(err)
	}
	if back, err := unmarshalDevice(item); err != nil || back.Sent != 5 {
		t.Fatalf("%+v %v", back, err)
	}
	delete(item, "sent") // a row from before the director existed
	if old, err := unmarshalDevice(item); err != nil || old.Sent != 0 {
		t.Fatalf("%+v %v", old, err)
	}
}

func TestListScheduledIsTheClaimedPanelsWithSomethingAskedFor(t *testing.T) {
	f, ctx := NewFake(), context.Background()
	for _, tc := range []struct {
		thing, owner string
		games        []int64
	}{
		{"scoreboard-01", "sub-a", []int64{2026020001}}, // listed
		{"scoreboard-02", "sub-b", nil},                 // claimed, nothing asked for
		{"scoreboard-03", "", []int64{2026020001}},      // never claimed
	} {
		_ = f.Register(ctx, tc.thing)
		if tc.owner != "" {
			_ = f.Claim(ctx, tc.thing, tc.owner)
			_ = f.Update(ctx, Device{ThingName: tc.thing, Owner: tc.owner, Schedule: schedule.Panel{Games: tc.games}})
		} else {
			f.items[tc.thing] = Device{ThingName: tc.thing, Schedule: schedule.Panel{Games: tc.games}}
		}
	}
	got, err := f.ListScheduled(ctx)
	if err != nil || len(got) != 1 || got[0].ThingName != "scoreboard-01" {
		t.Fatalf("%+v %v", got, err)
	}
}

func TestMarkSentChangesTheGameTheStampAndTheMarkerAndNothingElse(t *testing.T) {
	f, ctx := NewFake(), context.Background()
	_ = f.Register(ctx, "scoreboard-7qf2")
	_ = f.Claim(ctx, "scoreboard-7qf2", "sub-a")
	lead := 30
	before := Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", Name: "Den", GameID: 1, ChosenAt: 10,
		Display: settings.Settings{CountdownLeadMin: &lead}, Schedule: schedule.Panel{Games: []int64{2026020001}}}
	_ = f.Update(ctx, before)
	if err := f.MarkSent(ctx, "scoreboard-7qf2", "sub-b", 2026020001, 20); !errors.Is(err, ErrNotOwner) {
		t.Fatalf("another owner: err = %v, want ErrNotOwner", err)
	}
	if err := f.MarkSent(ctx, "scoreboard-7qf2", "sub-a", 2026020001, 20); err != nil {
		t.Fatal(err)
	}
	after, _, _ := f.Get(ctx, "scoreboard-7qf2")
	want := before
	want.GameID, want.ChosenAt, want.Sent = 2026020001, 20, 2026020001
	if !reflect.DeepEqual(after, want) {
		t.Errorf("got %+v\nwant %+v", after, want)
	}
	// The owner's own save leaves the marker alone: it is the director's
	// record, not a setting.
	_ = f.Update(ctx, Device{ThingName: "scoreboard-7qf2", Owner: "sub-a", GameID: 7})
	if d, _, _ := f.Get(ctx, "scoreboard-7qf2"); d.Sent != 2026020001 || d.GameID != 7 {
		t.Errorf("after the owner's update: %+v", d)
	}
	// Releasing the panel clears it with everything else.
	_ = f.Unbind(ctx, "scoreboard-7qf2", "sub-a")
	if d, _, _ := f.Get(ctx, "scoreboard-7qf2"); d.Sent != 0 {
		t.Errorf("the next owner inherits sent=%d", d.Sent)
	}
}
