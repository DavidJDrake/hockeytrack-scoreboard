package templates

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

func TestDecodeIsStrictAndEveryBoundHoldsAtItsEdge(t *testing.T) {
	ids := func(n int) string {
		parts := make([]string, n)
		for i := range parts {
			parts[i] = fmt.Sprint(i + 1)
		}
		return strings.Join(parts, ",")
	}
	for name, tc := range map[string]struct {
		body string
		want error
	}{
		"not JSON":                   {`name`, ErrMalformed},
		"a list":                     {`[]`, ErrMalformed},
		"an unknown key":             {`{"name":"x","games":[],"extra":1}`, ErrMalformed},
		"an id from the client":      {`{"id":"abc","name":"x","games":[]}`, ErrMalformed},
		"trailing data":              {`{"name":"x","games":[]} {}`, ErrMalformed},
		"an id of zero":              {`{"name":"x","games":[0]}`, ErrMalformed},
		"the same game twice":        {`{"name":"x","games":[1,1]}`, ErrMalformed},
		"one game over the bound":    {`{"name":"x","games":[` + ids(MaxGames+1) + `]}`, ErrMalformed},
		"no name":                    {`{"games":[1]}`, ErrBadName},
		"a blank name":               {`{"name":"   ","games":[1]}`, ErrBadName},
		"a control character":        {`{"name":"line\nbreak","games":[1]}`, ErrBadName},
		"an escape sequence":         {`{"name":"\u001b[31mred","games":[1]}`, ErrBadName},
		"one rune over the bound":    {`{"name":"` + strings.Repeat("é", MaxNameRunes+1) + `","games":[1]}`, ErrBadName},
		"exactly the bound":          {`{"name":"` + strings.Repeat("é", MaxNameRunes) + `","games":[` + ids(MaxGames) + `]}`, nil},
		"one character, no games":    {`{"name":"x"}`, nil},
		"a name with inner spaces":   {`{"name":"Habs at home","games":[2026020001]}`, nil},
		"null games become no games": {`{"name":"x","games":null}`, nil},
	} {
		got, err := Decode([]byte(tc.body))
		if !errors.Is(err, tc.want) {
			t.Errorf("%s: got %v, want %v", name, err, tc.want)
		}
		if err == nil && (got.ID != "" || got.Games == nil) {
			t.Errorf("%s: %+v", name, got)
		}
	}
}

func TestNewIDIsTheServersAndDoesNotRepeat(t *testing.T) {
	a, b := NewID(), NewID()
	if len(a) != 2*idBytes || a == b {
		t.Errorf("%q %q", a, b)
	}
}

func TestTheFakeKeepsEachOwnersTemplatesApart(t *testing.T) {
	ctx := context.Background()
	st := NewFake()
	if err := st.Create(ctx, "sub-a", Template{ID: "t-1", Name: "Habs", Games: []int64{1}}); err != nil {
		t.Fatal(err)
	}
	if err := st.Create(ctx, "sub-a", Template{ID: "t-1", Name: "again"}); !errors.Is(err, ErrExists) {
		t.Errorf("a second create of one id: %v", err)
	}
	// The same id under another owner is another row: the key is the pair.
	// Nothing sub-b does reaches sub-a's template.
	if err := st.Replace(ctx, "sub-b", Template{ID: "t-1", Name: "mine now"}); !errors.Is(err, ErrNotFound) {
		t.Errorf("replace across owners: %v", err)
	}
	if err := st.Delete(ctx, "sub-b", "t-1"); !errors.Is(err, ErrNotFound) {
		t.Errorf("delete across owners: %v", err)
	}
	list, _ := st.List(ctx, "sub-b")
	if len(list) != 0 || list == nil {
		t.Errorf("sub-b sees %v", list)
	}
	list, _ = st.List(ctx, "sub-a")
	if len(list) != 1 || list[0].Name != "Habs" {
		t.Errorf("sub-a sees %v", list)
	}
	_ = st.Create(ctx, "sub-a", Template{ID: "t-0", Name: "Away games"})
	_ = st.Create(ctx, "sub-a", Template{ID: "t-2", Name: "Habs"})
	list, _ = st.List(ctx, "sub-a")
	if fmt.Sprintf("%s %s %s", list[0].ID, list[1].ID, list[2].ID) != "t-0 t-1 t-2" {
		t.Errorf("by name then id: %v", list)
	}
	if fmt.Sprint(GamesOf(list)) != "map[t-0:[] t-1:[1] t-2:[]]" {
		t.Errorf("%v", GamesOf(list))
	}
}

func TestItemRoundTripsAndDamageReadsAsAnEmptyTemplate(t *testing.T) {
	in := Template{ID: "abc", Name: "Habs", Games: []int64{2026020001, 2026020002}}
	item := marshalTemplate("sub-a", in)
	if item["owner"].(*types.AttributeValueMemberS).Value != "sub-a" || item["templateId"].(*types.AttributeValueMemberS).Value != "abc" {
		t.Errorf("key %v", item)
	}
	got, err := unmarshalTemplate(item)
	if err != nil || fmt.Sprint(got) != fmt.Sprint(in) {
		t.Errorf("%+v %v", got, err)
	}
	for _, damaged := range []string{"", "{", `[0]`, `["1"]`, `[1,1]`} {
		item["games"] = &types.AttributeValueMemberS{Value: damaged}
		if got, err := unmarshalTemplate(item); err != nil || len(got.Games) != 0 || got.Games == nil {
			t.Errorf("%q: %+v %v", damaged, got, err)
		}
	}
	delete(item, "games")
	if got, err := unmarshalTemplate(item); err != nil || got.Games == nil {
		t.Errorf("absent: %+v %v", got, err)
	}
	if got := storedGames(nil); got != "[]" {
		t.Errorf("nil stores as %q", got)
	}
}
