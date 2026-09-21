// Command summary publishes, once a minute, one retained document with every
// current game's score (internal/summary). It reads the reducer's table and
// writes one topic; it fetches nothing and takes no input, so there is
// nothing an event can make it do.
//
// On a schedule rather than from the reducer, which knows the moment a score
// changes: two reducers finishing together would each publish a list missing
// the other's goal, and whichever landed last would be retained. One writer
// cannot race itself, and a minute is soon enough for a line about a game
// the panel is not showing.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/iotdataplane"

	"hockeytrack-scoreboard/internal/gamestore"
	"hockeytrack-scoreboard/internal/iotpub"
	"hockeytrack-scoreboard/internal/summary"
)

const topic = "hockeytrack/games/summary"

// An unchanged list is published again after this long anyway. The retained
// copy is the only one a panel that has just connected will get; if it were
// ever lost, this is how long a panel would go without.
const republishAfter = 10 * time.Minute

// publisher remembers what it last sent, for as long as Lambda keeps this
// process. Most minutes of most days nothing has changed, and every panel
// would be woken to be told so.
type publisher struct {
	store gamestore.Store
	pub   iotpub.Publisher
	now   func() time.Time

	lastGames []byte
	lastAt    time.Time
}

func (p *publisher) run(ctx context.Context) error {
	states, err := p.store.ListActive(ctx)
	if err != nil {
		return err
	}
	now := p.now()
	doc := summary.Build(states, now)
	games, err := json.Marshal(doc.Games)
	if err != nil {
		return err
	}
	if bytes.Equal(games, p.lastGames) && now.Sub(p.lastAt) < republishAfter {
		return nil
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	if err := p.pub.Publish(ctx, topic, out, true); err != nil {
		return err // and nothing is remembered, so the next minute tries again
	}
	p.lastGames, p.lastAt = games, now
	return nil
}

func main() {
	ctx := context.Background()
	cfg, err := config.LoadDefaultConfig(ctx)
	if err != nil {
		slog.Error("aws config", "err", err)
		os.Exit(1)
	}
	endpoint := os.Getenv("IOT_ENDPOINT")
	iot := iotdataplane.NewFromConfig(cfg, func(o *iotdataplane.Options) { o.BaseEndpoint = &endpoint })
	p := &publisher{
		store: gamestore.NewDynamo(dynamodb.NewFromConfig(cfg), os.Getenv("GAMES_TABLE")),
		pub:   iotpub.NewIoT(iot),
		now:   time.Now,
	}
	lambda.Start(func(ctx context.Context) error { return p.run(ctx) })
}
