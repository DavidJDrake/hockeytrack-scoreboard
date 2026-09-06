package gamestore

import (
	"context"
	"errors"
	"strconv"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"

	"hockeytrack-scoreboard/internal/reduce"
)

// Dynamo is a Store backed by a DynamoDB table with partition key gameId
// (N). Items carry an expiresAt (N) attribute for TTL cleanup.
type Dynamo struct {
	client *dynamodb.Client
	table  string
	now    func() time.Time
}

// NewDynamo returns a Dynamo store backed by client and table.
func NewDynamo(client *dynamodb.Client, table string) *Dynamo {
	return &Dynamo{client: client, table: table, now: time.Now}
}

// item wraps reduce.State with the DynamoDB-only TTL attribute.
type item struct {
	reduce.State
	ExpiresAt int64 `dynamodbav:"expiresAt"`
}

func (d *Dynamo) Get(ctx context.Context, gameID int64) (reduce.State, bool, error) {
	out, err := d.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName:      aws.String(d.table),
		Key:            map[string]types.AttributeValue{"gameId": &types.AttributeValueMemberN{Value: itoa(gameID)}},
		ConsistentRead: aws.Bool(true),
	})
	if err != nil {
		return reduce.State{}, false, err
	}
	if out.Item == nil {
		return reduce.State{}, false, nil
	}
	var it item
	if err := attributevalue.UnmarshalMap(out.Item, &it); err != nil {
		return reduce.State{}, false, err
	}
	return it.State, true, nil
}

func (d *Dynamo) Put(ctx context.Context, s reduce.State) error {
	expected := s.Version
	s.Version++
	av, err := attributevalue.MarshalMap(item{State: s, ExpiresAt: d.now().Add(48 * time.Hour).Unix()})
	if err != nil {
		return err
	}
	in := &dynamodb.PutItemInput{TableName: aws.String(d.table), Item: av}
	if expected == 0 {
		in.ConditionExpression = aws.String("attribute_not_exists(gameId)")
	} else {
		in.ConditionExpression = aws.String("version = :v")
		in.ExpressionAttributeValues = map[string]types.AttributeValue{":v": &types.AttributeValueMemberN{Value: itoa(expected)}}
	}
	if _, err := d.client.PutItem(ctx, in); err != nil {
		var ccf *types.ConditionalCheckFailedException
		if errors.As(err, &ccf) {
			return ErrConflict
		}
		return err
	}
	return nil
}

func (d *Dynamo) ListActive(ctx context.Context) ([]reduce.State, error) {
	cutoff := d.now().Add(-6 * time.Hour).UnixMilli()
	p := dynamodb.NewScanPaginator(d.client, &dynamodb.ScanInput{
		TableName:                aws.String(d.table),
		FilterExpression:         aws.String("#st <> :final OR asOf > :cutoff"),
		ExpressionAttributeNames: map[string]string{"#st": "state"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":final":  &types.AttributeValueMemberS{Value: "FINAL"},
			":cutoff": &types.AttributeValueMemberN{Value: itoa(cutoff)},
		},
	})
	var out []reduce.State
	for p.HasMorePages() {
		page, err := p.NextPage(ctx)
		if err != nil {
			return nil, err
		}
		for _, raw := range page.Items {
			var it item
			if err := attributevalue.UnmarshalMap(raw, &it); err != nil {
				return nil, err
			}
			out = append(out, it.State)
		}
	}
	return out, nil
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }
