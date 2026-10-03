package presence

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iot"
	"github.com/aws/aws-sdk-go-v2/service/iot/types"
)

func TestTheQueryNamesEachPanelAndNothingThatCouldReadAsSyntax(t *testing.T) {
	for _, tc := range []struct {
		in   []string
		want string
	}{
		// Quoted as a phrase: the syntax makes a hyphen an operator, and
		// every real name has one.
		{[]string{"scoreboard-01"}, `thingName:("scoreboard-01")`},
		{[]string{"scoreboard-01", "scoreboard_02"}, `thingName:("scoreboard-01" OR "scoreboard_02")`},
		// A name is data. One that the index's syntax would read as an
		// operator, a wildcard, a group or the end of a phrase is left out,
		// not escaped.
		{[]string{"-scoreboard", "score*", "a b", "x)", `q"`, "ok-1"}, `thingName:("ok-1")`},
		{[]string{"-scoreboard"}, ""},
		{nil, ""},
	} {
		if got := Query(tc.in); got != tc.want {
			t.Errorf("Query(%q) = %q, want %q", tc.in, got, tc.want)
		}
	}
}

func TestTheIndexRecordReadsAsSeenOrAsUnknown(t *testing.T) {
	ms := int64(1789871240471)
	on := seenOf(&types.ThingConnectivity{Connected: aws.Bool(true), Timestamp: &ms})
	if !on.Connected || !on.At.Equal(time.UnixMilli(ms)) {
		t.Errorf("connected: %+v", on)
	}
	off := seenOf(&types.ThingConnectivity{Connected: aws.Bool(false), Timestamp: &ms})
	if off.Connected || !off.At.Equal(time.UnixMilli(ms)) {
		t.Errorf("disconnected: %+v", off)
	}
	// AWS's documented shape for a thing that never connected since indexing
	// began: connected=false and no timestamp. That is unknown, not old.
	never := seenOf(&types.ThingConnectivity{Connected: aws.Bool(false)})
	if never.Connected || !never.At.IsZero() {
		t.Errorf("never connected: %+v", never)
	}
	if got := seenOf(nil); got != (Seen{}) {
		t.Errorf("no record: %+v", got)
	}
}

// index is a Searcher for tests: every call is recorded, and the answers are
// handed out in order, one per call.
type index struct {
	calls   []iot.SearchIndexInput
	answers []*iot.SearchIndexOutput
}

func (x *index) SearchIndex(_ context.Context, in *iot.SearchIndexInput, _ ...func(*iot.Options)) (*iot.SearchIndexOutput, error) {
	x.calls = append(x.calls, *in)
	if len(x.answers) == 0 {
		return &iot.SearchIndexOutput{}, nil
	}
	a := x.answers[0]
	x.answers = x.answers[1:]
	return a, nil
}

func doc(name string, ms int64) types.ThingDocument {
	return types.ThingDocument{ThingName: aws.String(name),
		Connectivity: &types.ThingConnectivity{Connected: aws.Bool(false), Timestamp: aws.Int64(ms)}}
}

func TestMoreNamesThanTheIndexTakesInOneQueryAreSeveralQueries(t *testing.T) {
	names := make([]string, 0, maxTerms+1)
	for i := 0; i < maxTerms+1; i++ {
		names = append(names, fmt.Sprintf("scoreboard-%02d", i))
	}
	x := &index{}
	if _, err := NewIoT(x).Lookup(context.Background(), names); err != nil {
		t.Fatal(err)
	}
	if len(x.calls) != 2 {
		t.Fatalf("%d names made %d calls, want 2", len(names), len(x.calls))
	}
	if got, want := *x.calls[0].QueryString, Query(names[:maxTerms]); got != want {
		t.Errorf("first query %q, want %q", got, want)
	}
	if got, want := *x.calls[1].QueryString, Query(names[maxTerms:]); got != want {
		t.Errorf("second query %q, want %q", got, want)
	}
	for _, c := range x.calls {
		if *c.IndexName != Index || c.NextToken != nil {
			t.Errorf("call %+v: want index %s and no token", c, Index)
		}
	}
}

func TestAQueryThatPagesIsReadToTheEndAndMerged(t *testing.T) {
	x := &index{answers: []*iot.SearchIndexOutput{
		{Things: []types.ThingDocument{doc("scoreboard-01", 1000)}, NextToken: aws.String("more")},
		{Things: []types.ThingDocument{doc("scoreboard-02", 2000)}},
	}}
	got, err := NewIoT(x).Lookup(context.Background(), []string{"scoreboard-01", "scoreboard-02"})
	if err != nil {
		t.Fatal(err)
	}
	if len(x.calls) != 2 || x.calls[0].NextToken != nil || x.calls[1].NextToken == nil || *x.calls[1].NextToken != "more" {
		t.Fatalf("calls %+v: want the second to carry the first's token", x.calls)
	}
	if len(got) != 2 || !got["scoreboard-01"].At.Equal(time.UnixMilli(1000)) || !got["scoreboard-02"].At.Equal(time.UnixMilli(2000)) {
		t.Errorf("merged %+v", got)
	}
}

func TestADocumentWithoutANameIsDroppedAndAnUnqueryableBatchIsNotAsked(t *testing.T) {
	x := &index{answers: []*iot.SearchIndexOutput{
		{Things: []types.ThingDocument{
			{Connectivity: &types.ThingConnectivity{Connected: aws.Bool(true)}},
			doc("scoreboard-01", 1000),
		}},
	}}
	got, err := NewIoT(x).Lookup(context.Background(), []string{"scoreboard-01"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got["scoreboard-01"].At.IsZero() {
		t.Errorf("a nameless document was kept, or the named one lost: %+v", got)
	}
	// A batch with nothing queryable in it makes no call at all, rather than
	// a call with an empty query the index would answer with everything.
	x = &index{}
	got, err = NewIoT(x).Lookup(context.Background(), []string{"-scoreboard", "score*"})
	if err != nil || len(got) != 0 || len(x.calls) != 0 {
		t.Errorf("unqueryable batch: got %+v, err %v, calls %d", got, err, len(x.calls))
	}
}

func TestTheFakeAnswersOnlyForNamesItHolds(t *testing.T) {
	f := Fake{"scoreboard-01": {Connected: true}}
	got, err := f.Lookup(context.Background(), []string{"scoreboard-01", "scoreboard-02"})
	if err != nil || len(got) != 1 || !got["scoreboard-01"].Connected {
		t.Fatalf("%+v %v", got, err)
	}
}
