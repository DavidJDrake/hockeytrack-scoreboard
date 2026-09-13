package enroll

import (
	"errors"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

func reason(code string) types.CancellationReason {
	return types.CancellationReason{Code: aws.String(code)}
}

// TestRotateErrorIndexMapping drives rotateError itself, not just the
// reasonFailed helper it calls. A test of reasonFailed in isolation can only
// prove the helper reads whatever index it is given -- it cannot catch
// rotateError asking for the wrong index, which is the actual defect this
// guards against (a prior fix swapped 1 and 2 at the call site and every
// other test in this package, including a reasonFailed-only test, stayed
// green). RotateCode itself is still untested here: that would need a real
// or fabricated *dynamodb.Client, which this package deliberately does not
// build. What's covered is everything downstream of "TransactWriteItems
// returned this TransactionCanceledException" -- not whether RotateCode
// actually constructs its TransactItems in the order the comment there
// claims.
func TestRotateErrorIndexMapping(t *testing.T) {
	ok := reason("None")
	failed := reason("ConditionalCheckFailed")

	t.Run("only the new reservation failed", func(t *testing.T) {
		err := &types.TransactionCanceledException{
			CancellationReasons: []types.CancellationReason{ok, failed, ok},
		}
		if got := rotateError(err); !errors.Is(got, ErrCodeTaken) {
			t.Errorf("rotateError() = %v, want ErrCodeTaken", got)
		}
	})

	t.Run("only the enrollment update failed", func(t *testing.T) {
		err := &types.TransactionCanceledException{
			CancellationReasons: []types.CancellationReason{ok, ok, failed},
		}
		if got := rotateError(err); !errors.Is(got, ErrAlreadyClaimed) {
			t.Errorf("rotateError() = %v, want ErrAlreadyClaimed", got)
		}
	})

	t.Run("both failed: already-claimed wins because it is terminal", func(t *testing.T) {
		err := &types.TransactionCanceledException{
			CancellationReasons: []types.CancellationReason{ok, failed, failed},
		}
		if got := rotateError(err); !errors.Is(got, ErrAlreadyClaimed) {
			t.Errorf("rotateError() = %v, want ErrAlreadyClaimed", got)
		}
	})

	t.Run("a short reason slice returns the original error rather than panicking", func(t *testing.T) {
		err := &types.TransactionCanceledException{
			CancellationReasons: []types.CancellationReason{ok},
		}
		if got := rotateError(err); got != error(err) {
			t.Errorf("rotateError() = %v, want the original error unchanged", got)
		}
	})

	t.Run("a nil reason slice returns the original error rather than panicking", func(t *testing.T) {
		err := &types.TransactionCanceledException{CancellationReasons: nil}
		if got := rotateError(err); got != error(err) {
			t.Errorf("rotateError() = %v, want the original error unchanged", got)
		}
	})

	t.Run("an unrelated error is returned unchanged", func(t *testing.T) {
		err := errors.New("boom")
		if got := rotateError(err); got != error(err) {
			t.Errorf("rotateError() = %v, want the original error unchanged", got)
		}
	})
}

// TestReasonFailedIndexMapping is the lower-level complement: it checks the
// index-reading primitive on its own, including the length guard. It cannot
// by itself catch rotateError asking for the wrong index -- that is
// TestRotateErrorIndexMapping's job.
func TestReasonFailedIndexMapping(t *testing.T) {
	ok := reason("None")
	failed := reason("ConditionalCheckFailed")

	cases := []struct {
		name    string
		reasons []types.CancellationReason
		index   int
		want    bool
	}{
		{"all succeeded", []types.CancellationReason{ok, ok, ok}, 1, false},
		{"index 1 failed", []types.CancellationReason{ok, failed, ok}, 1, true},
		{"index 1 failed does not read as index 2", []types.CancellationReason{ok, failed, ok}, 2, false},
		{"index 2 failed", []types.CancellationReason{ok, ok, failed}, 2, true},
		{"index 2 failed does not read as index 1", []types.CancellationReason{ok, ok, failed}, 1, false},
		{"short slice does not panic or false-positive", []types.CancellationReason{ok}, 2, false},
		{"nil slice does not panic", nil, 0, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := reasonFailed(c.reasons, c.index); got != c.want {
				t.Errorf("reasonFailed(%+v, %d) = %v, want %v", c.reasons, c.index, got, c.want)
			}
		})
	}
}
