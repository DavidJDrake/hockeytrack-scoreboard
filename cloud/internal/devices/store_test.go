package devices

import (
	"context"
	"errors"
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

func TestClaimBindsAnUnclaimedDeviceExactlyOnce(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	if err := st.Register(ctx, "scoreboard-01", "01"); err != nil {
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
	for _, tc := range []struct{ thing, code, owner string }{
		{"scoreboard-01", "01", "user-a"},
		{"scoreboard-02", "02", "user-b"},
		{"scoreboard-03", "03", "user-a"},
	} {
		if err := st.Register(ctx, tc.thing, tc.code); err != nil {
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
	_ = st.Register(ctx, "scoreboard-01", "01")
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

func TestByCodeFindsOnlyUnclaimedDevices(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01", "7QF2")
	if d, found, _ := st.ByCode(ctx, "7QF2"); !found || d.ThingName != "scoreboard-01" {
		t.Fatalf("ByCode before claim: found=%v thing=%q", found, d.ThingName)
	}
	_ = st.Claim(ctx, "scoreboard-01", "user-a")
	if _, found, _ := st.ByCode(ctx, "7QF2"); found {
		t.Error("ByCode found a claimed device; a used code must stop resolving")
	}
}

func TestUpdateEnforcesOwnershipAndImmutability(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01", "7QF2")
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

	// Code and ThingName must remain immutable: attempting to change them has no effect.
	if err := st.Update(ctx, Device{ThingName: "scoreboard-01", Owner: "user-a", Code: "XXXX"}); err != nil {
		t.Fatalf("update with different Code: %v", err)
	}
	d, _, _ = st.Get(ctx, "scoreboard-01")
	if d.Code != "7QF2" {
		t.Errorf("Code was mutated to %q, want 7QF2 (immutable)", d.Code)
	}
}

func TestItemRoundTripsThroughDynamoAttributes(t *testing.T) {
	in := Device{ThingName: "scoreboard-7qf2", Owner: "sub-123", Name: "Living room", GameID: 2026020001, Code: "7QF2"}
	item, err := marshalDevice(in)
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"thingName", "owner", "name", "gameId", "code"} {
		if _, ok := item[key]; !ok {
			t.Errorf("marshalled item is missing %q", key)
		}
	}
	out, err := unmarshalDevice(item)
	if err != nil {
		t.Fatal(err)
	}
	if out != in {
		t.Errorf("round trip = %+v, want %+v", out, in)
	}
}

func TestAnUnclaimedDeviceMarshalsWithoutAnOwnerAttribute(t *testing.T) {
	item, err := marshalDevice(Device{ThingName: "scoreboard-7qf2", Code: "7QF2"})
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
		if err := st.Register(ctx, name, "01"); !errors.Is(err, ErrInvalidThingName) {
			t.Errorf("Register(%q) err = %v, want ErrInvalidThingName", name, err)
		}
	}
	if _, found, _ := st.Get(ctx, ""); found {
		t.Error("an invalid thing name must not be stored")
	}
	for _, name := range []string{"scoreboard-01", "scoreboard_01", "ABC123"} {
		if err := st.Register(ctx, name, "01"); err != nil {
			t.Errorf("Register(%q) with a valid name: %v", name, err)
		}
	}
}

func TestByCodeErrorsOnDuplicateUnclaimedCodes(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01", "DUPE")
	_ = st.Register(ctx, "scoreboard-02", "DUPE")
	if _, _, err := st.ByCode(ctx, "DUPE"); !errors.Is(err, ErrDuplicateCode) {
		t.Errorf("ByCode with two unclaimed rows sharing a code err = %v, want ErrDuplicateCode", err)
	}
}

func TestByCodeIgnoresClaimedDuplicatesWhenOnlyOneIsUnclaimed(t *testing.T) {
	st, ctx := NewFake(), context.Background()
	_ = st.Register(ctx, "scoreboard-01", "DUPE")
	_ = st.Register(ctx, "scoreboard-02", "DUPE")
	_ = st.Claim(ctx, "scoreboard-01", "user-a")
	d, found, err := st.ByCode(ctx, "DUPE")
	if err != nil || !found || d.ThingName != "scoreboard-02" {
		t.Fatalf("ByCode = %+v, found=%v, err=%v; want scoreboard-02/true/nil", d, found, err)
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
