package devices

import (
	"context"
	"errors"
	"testing"
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
