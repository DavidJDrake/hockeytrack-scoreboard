package presence

import (
	"context"
	"regexp"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iot"
	"github.com/aws/aws-sdk-go-v2/service/iot/types"
)

// Index is the fleet index AWS builds for every thing in the account. Its
// name is fixed by the service; the ARN the IAM grants name ends in it.
const Index = "AWS_Things"

// maxTerms is the fleet index's own limit on terms in one query (query
// syntax, "the maximum number of terms in a query is twelve"), so a lookup
// for more names than this is several queries, not one that fails.
const maxTerms = 12

// queryable is what a thing name may look like to be put in a query. The
// index's syntax gives special meaning to a leading hyphen, wildcards,
// quotes, parentheses and a list of punctuation; a name is data, so one that
// could read as syntax is left out of the query and comes back as unknown
// rather than being escaped and trusted. devices.Register accepts a strict
// subset of this already, so every name it has ever stored qualifies. No
// double quote passes, which is what lets Query wrap each name in them.
var queryable = regexp.MustCompile(`^[A-Za-z0-9_][A-Za-z0-9_-]*$`)

// Searcher is the one call Lookup makes on AWS. *iot.Client satisfies it;
// presence_test.go drives Lookup with a fake so the batching, the paging and
// the skips are checked without an account.
type Searcher interface {
	SearchIndex(ctx context.Context, in *iot.SearchIndexInput, opts ...func(*iot.Options)) (*iot.SearchIndexOutput, error)
}

// IoT is the Source backed by the fleet index. iot:SearchIndex on the index
// is the whole of what it needs.
type IoT struct {
	client Searcher
}

// NewIoT returns a Source that reads the fleet index through the given
// client.
func NewIoT(client Searcher) *IoT { return &IoT{client: client} }

// Query builds the index query for a batch of names, thingName:("a" OR "b"),
// leaving out any name that is not queryable. Each name is quoted as a
// phrase: the syntax lists the hyphen as an operator (thingName:(tv* AND
// -plasma) is the documented example) and every real panel name is
// hyphenated, so a bare scoreboard-01 leans on the parser reading a
// mid-term hyphen as part of the term, which the documentation does not
// promise. A phrase is matched as written. The index did not exist in the
// account when this was written (indexing was OFF on 2026-09-30), so the
// first live result is recorded on SCO-33, not here. Exported so a test can
// pin the shape without calling AWS.
func Query(things []string) string {
	terms := make([]string, 0, len(things))
	for _, name := range things {
		if queryable.MatchString(name) {
			terms = append(terms, `"`+name+`"`)
		}
	}
	if len(terms) == 0 {
		return ""
	}
	return "thingName:(" + strings.Join(terms, " OR ") + ")"
}

func (x *IoT) Lookup(ctx context.Context, things []string) (map[string]Seen, error) {
	out := map[string]Seen{}
	for start := 0; start < len(things); start += maxTerms {
		batch := things[start:min(start+maxTerms, len(things))]
		q := Query(batch)
		if q == "" {
			continue
		}
		var next *string
		for {
			res, err := x.client.SearchIndex(ctx, &iot.SearchIndexInput{
				IndexName:   aws.String(Index),
				QueryString: aws.String(q),
				NextToken:   next,
			})
			if err != nil {
				return nil, err
			}
			for _, doc := range res.Things {
				if doc.ThingName == nil {
					continue
				}
				out[*doc.ThingName] = seenOf(doc.Connectivity)
			}
			if next = res.NextToken; next == nil {
				break
			}
		}
	}
	return out, nil
}

// seenOf reads the index's connectivity record. AWS documents that a thing
// which never connected, or was already off the broker for over an hour when
// indexing was turned on, comes back connected=false with no timestamp: that
// is the zero At, unknown, and nothing downstream may read it as old.
func seenOf(c *types.ThingConnectivity) Seen {
	if c == nil {
		return Seen{}
	}
	s := Seen{Connected: c.Connected != nil && *c.Connected}
	if c.Timestamp != nil {
		s.At = time.UnixMilli(*c.Timestamp).UTC()
	}
	return s
}
