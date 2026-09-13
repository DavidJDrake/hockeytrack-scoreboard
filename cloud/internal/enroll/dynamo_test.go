package enroll

import (
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

// TestReasonFailedIndexMapping locks down the index mapping RotateCode
// depends on: index 1 is the new reservation, index 2 is the enrollment
// update. This is the part the Fake cannot exercise, since it never builds a
// types.CancellationReason -- so it is also the part a regression here would
// slip past every other test in this package. It needs no AWS client:
// CancellationReason is a plain struct, not a network call.
func TestReasonFailedIndexMapping(t *testing.T) {
	ok := &types.CancellationReason{Code: aws.String("None")}
	failed := &types.CancellationReason{Code: aws.String("ConditionalCheckFailed")}

	cases := []struct {
		name    string
		reasons []types.CancellationReason
		index   int
		want    bool
	}{
		{"all succeeded", []types.CancellationReason{*ok, *ok, *ok}, 1, false},
		{"index 1 failed (new code collided)", []types.CancellationReason{*ok, *failed, *ok}, 1, true},
		{"index 1 failed does not read as index 2", []types.CancellationReason{*ok, *failed, *ok}, 2, false},
		{"index 2 failed (already claimed)", []types.CancellationReason{*ok, *ok, *failed}, 2, true},
		{"index 2 failed does not read as index 1", []types.CancellationReason{*ok, *ok, *failed}, 1, false},
		{"both failed, asking for index 2", []types.CancellationReason{*ok, *failed, *failed}, 2, true},
		{"short slice does not panic or false-positive", []types.CancellationReason{*ok}, 2, false},
		{"empty slice does not panic", nil, 0, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := reasonFailed(c.reasons, c.index); got != c.want {
				t.Errorf("reasonFailed(%+v, %d) = %v, want %v", c.reasons, c.index, got, c.want)
			}
		})
	}
}
