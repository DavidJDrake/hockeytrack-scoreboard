package devices

import (
	"context"
	"errors"
	"fmt"
	"strconv"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"

	"hockeytrack-scoreboard/internal/schedule"
	"hockeytrack-scoreboard/internal/settings"
)

// Dynamo is the DynamoDB-backed Store. Items carry thingName (S, partition
// key), owner (S), name (S), and gameId (N). GSI owner-index is keyed on
// owner.
type Dynamo struct {
	client *dynamodb.Client
	table  string
}

// NewDynamo returns a Store backed by the given table.
func NewDynamo(client *dynamodb.Client, table string) *Dynamo {
	return &Dynamo{client: client, table: table}
}

// marshalDevice writes a Device as an item. An unclaimed device omits owner
// entirely rather than storing an empty string: Claim's condition expression
// is attribute_not_exists(owner), and an empty string is an attribute that
// exists. Omitting it also keeps owner-index sparse, so unclaimed devices
// don't pile onto one partition key.
func marshalDevice(d Device) (map[string]types.AttributeValue, error) {
	item := map[string]types.AttributeValue{
		"thingName": &types.AttributeValueMemberS{Value: d.ThingName},
		"name":      &types.AttributeValueMemberS{Value: d.Name},
		"gameId":    &types.AttributeValueMemberN{Value: strconv.FormatInt(d.GameID, 10)},
		"chosenAt":  &types.AttributeValueMemberN{Value: strconv.FormatInt(d.ChosenAt, 10)},
		"display":   &types.AttributeValueMemberS{Value: settings.Stored(d.Display)},
		"schedule":  &types.AttributeValueMemberS{Value: schedule.Stored(d.Schedule)},
		"wake":      &types.AttributeValueMemberS{Value: settings.StoredWake(d.Wake)},
		"sent":      &types.AttributeValueMemberN{Value: strconv.FormatInt(d.Sent, 10)},
	}
	if d.Owner != "" {
		item["owner"] = &types.AttributeValueMemberS{Value: d.Owner}
	}
	return item, nil
}

// unmarshalDevice reads a Device back out of an item. Written as plain field
// extraction rather than attributevalue.UnmarshalMap: owner is genuinely
// absent for an unclaimed device, and decoding that into a non-pointer string
// field needs no special handling here, so the extra machinery buys nothing.
func unmarshalDevice(item map[string]types.AttributeValue) (Device, error) {
	d := Device{}
	var err error
	if d.ThingName, err = attrS(item, "thingName"); err != nil {
		return Device{}, err
	}
	if d.Name, err = attrS(item, "name"); err != nil {
		return Device{}, err
	}
	if owner, ok := item["owner"]; ok {
		s, ok := owner.(*types.AttributeValueMemberS)
		if !ok {
			return Device{}, fmt.Errorf("devices: owner attribute is not a string")
		}
		d.Owner = s.Value
	}
	gameID, err := attrN(item, "gameId")
	if err != nil {
		return Device{}, err
	}
	d.GameID = gameID
	// Absent on every row written before this field existed: never chosen.
	if _, ok := item["chosenAt"]; ok {
		if d.ChosenAt, err = attrN(item, "chosenAt"); err != nil {
			return Device{}, err
		}
	}
	// Absent on older rows, and Load reads anything unreadable as "nothing
	// set": a damaged attribute must not take the panel off its owner's list.
	if attr, ok := item["display"].(*types.AttributeValueMemberS); ok {
		d.Display = settings.Load(attr.Value)
	}
	// The same: absent on older rows, and anything unreadable is "nothing
	// asked for".
	d.Schedule = schedule.Load("")
	if attr, ok := item["schedule"].(*types.AttributeValueMemberS); ok {
		d.Schedule = schedule.Load(attr.Value)
	}
	if attr, ok := item["wake"].(*types.AttributeValueMemberS); ok {
		d.Wake = settings.LoadWake(attr.Value)
	}
	// Absent on every row the director has not written yet: never sent.
	if _, ok := item["sent"]; ok {
		if d.Sent, err = attrN(item, "sent"); err != nil {
			return Device{}, err
		}
	}
	return d, nil
}

func attrS(item map[string]types.AttributeValue, key string) (string, error) {
	av, ok := item[key]
	if !ok {
		return "", fmt.Errorf("devices: item is missing %q", key)
	}
	s, ok := av.(*types.AttributeValueMemberS)
	if !ok {
		return "", fmt.Errorf("devices: %q attribute is not a string", key)
	}
	return s.Value, nil
}

func attrN(item map[string]types.AttributeValue, key string) (int64, error) {
	av, ok := item[key]
	if !ok {
		return 0, fmt.Errorf("devices: item is missing %q", key)
	}
	n, ok := av.(*types.AttributeValueMemberN)
	if !ok {
		return 0, fmt.Errorf("devices: %q attribute is not a number", key)
	}
	return strconv.ParseInt(n.Value, 10, 64)
}

// condFailure turns a failed condition expression into the right sentinel: a
// conditional UpdateItem fails identically whether the item doesn't exist or
// it exists but fails the condition (wrong or missing owner), so the two
// cases are told apart by whether DynamoDB returned the item that failed the
// check. Callers request that via ReturnValuesOnConditionCheckFailure:
// ALL_OLD on the UpdateItem call, then pass the exception's Item here.
func condFailure(item map[string]types.AttributeValue, missing, present error) error {
	if item == nil {
		return missing
	}
	return present
}

func (x *Dynamo) Get(ctx context.Context, thingName string) (Device, bool, error) {
	out, err := x.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName: aws.String(x.table),
		Key:       map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: thingName}},
	})
	if err != nil {
		return Device{}, false, err
	}
	if out.Item == nil {
		return Device{}, false, nil
	}
	d, err := unmarshalDevice(out.Item)
	if err != nil {
		return Device{}, false, err
	}
	return d, true, nil
}

func (x *Dynamo) ListByOwner(ctx context.Context, owner string) ([]Device, error) {
	out, err := x.client.Query(ctx, &dynamodb.QueryInput{
		TableName:                 aws.String(x.table),
		IndexName:                 aws.String("owner-index"),
		KeyConditionExpression:    aws.String("#o = :o"),
		ExpressionAttributeNames:  map[string]string{"#o": "owner"},
		ExpressionAttributeValues: map[string]types.AttributeValue{":o": &types.AttributeValueMemberS{Value: owner}},
	})
	if err != nil {
		return nil, err
	}
	devs := make([]Device, 0, len(out.Items))
	for _, item := range out.Items {
		d, err := unmarshalDevice(item)
		if err != nil {
			return nil, err
		}
		devs = append(devs, d)
	}
	return devs, nil
}

func (x *Dynamo) Register(ctx context.Context, thingName string) error {
	if !validThingName(thingName) {
		return ErrInvalidThingName
	}
	item, err := marshalDevice(Device{ThingName: thingName})
	if err != nil {
		return err
	}
	_, err = x.client.PutItem(ctx, &dynamodb.PutItemInput{
		TableName:           aws.String(x.table),
		Item:                item,
		ConditionExpression: aws.String("attribute_not_exists(thingName)"),
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return nil // already registered; provisioning may retry
	}
	return err
}

func (x *Dynamo) Claim(ctx context.Context, thingName, owner string) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                           aws.String(x.table),
		Key:                                 map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: thingName}},
		UpdateExpression:                    aws.String("SET #o = :o"),
		ConditionExpression:                 aws.String("attribute_exists(thingName) AND attribute_not_exists(#o)"),
		ExpressionAttributeNames:            map[string]string{"#o": "owner"},
		ExpressionAttributeValues:           map[string]types.AttributeValue{":o": &types.AttributeValueMemberS{Value: owner}},
		ReturnValuesOnConditionCheckFailure: types.ReturnValuesOnConditionCheckFailureAllOld,
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		// The condition fails identically whether thingName doesn't exist or
		// it exists but already has an owner; ALL_OLD tells them apart.
		return condFailure(cond.Item, ErrNotFound, ErrAlreadyClaimed)
	}
	return err
}

func (x *Dynamo) Update(ctx context.Context, d Device) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                aws.String(x.table),
		Key:                      map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: d.ThingName}},
		UpdateExpression:         aws.String("SET #n = :n, gameId = :g, chosenAt = :c, display = :d, schedule = :s, wake = :w"),
		ConditionExpression:      aws.String("#o = :o"),
		ExpressionAttributeNames: map[string]string{"#n": "name", "#o": "owner"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":n": &types.AttributeValueMemberS{Value: d.Name},
			":g": &types.AttributeValueMemberN{Value: strconv.FormatInt(d.GameID, 10)},
			":c": &types.AttributeValueMemberN{Value: strconv.FormatInt(d.ChosenAt, 10)},
			":d": &types.AttributeValueMemberS{Value: settings.Stored(d.Display)},
			":s": &types.AttributeValueMemberS{Value: schedule.Stored(d.Schedule)},
			":w": &types.AttributeValueMemberS{Value: settings.StoredWake(d.Wake)},
			":o": &types.AttributeValueMemberS{Value: d.Owner},
		},
		ReturnValuesOnConditionCheckFailure: types.ReturnValuesOnConditionCheckFailureAllOld,
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		// The condition fails identically whether thingName doesn't exist or
		// it exists with a different owner; ALL_OLD tells them apart.
		return condFailure(cond.Item, ErrNotFound, ErrNotOwner)
	}
	return err
}

// ListScheduled scans the table: there is no index on "has a schedule", and
// the fleet is a handful of rows. The filter keeps unclaimed devices off the
// wire; whether a claimed one has anything asked for is decided in Go, from
// the parsed schedule, so a damaged attribute is "nothing asked for" here as
// everywhere else. The director's role (terraform/director.tf) is the only
// one granted Scan on this table.
func (x *Dynamo) ListScheduled(ctx context.Context) ([]Device, error) {
	var out []Device
	var start map[string]types.AttributeValue
	for {
		res, err := x.client.Scan(ctx, &dynamodb.ScanInput{
			TableName:                aws.String(x.table),
			FilterExpression:         aws.String("attribute_exists(#o)"),
			ExpressionAttributeNames: map[string]string{"#o": "owner"},
			ExclusiveStartKey:        start,
		})
		if err != nil {
			return nil, err
		}
		for _, item := range res.Items {
			d, err := unmarshalDevice(item)
			if err != nil {
				return nil, err
			}
			if !d.Schedule.IsZero() {
				out = append(out, d)
			}
		}
		if start = res.LastEvaluatedKey; len(start) == 0 {
			return out, nil
		}
	}
}

// MarkSent names exactly three attributes and reads none back. That is what
// lets the director's IAM policy (terraform/director.tf) list the attributes
// this write may touch: a request that named anything else -- the schedule,
// the settings, the owner -- would be refused by IAM before it reached the
// table, whatever this code did. No ReturnValuesOnConditionCheckFailure
// either, since a refused write must not hand the whole row back through a
// path the policy does not describe; the one thing the director needs to
// know is that the panel is no longer this owner's.
func (x *Dynamo) MarkSent(ctx context.Context, thingName, owner string, gameID, chosenAt int64) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                aws.String(x.table),
		Key:                      map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: thingName}},
		UpdateExpression:         aws.String("SET gameId = :g, chosenAt = :c, sent = :g"),
		ConditionExpression:      aws.String("#o = :o"),
		ExpressionAttributeNames: map[string]string{"#o": "owner"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":g": &types.AttributeValueMemberN{Value: strconv.FormatInt(gameID, 10)},
			":c": &types.AttributeValueMemberN{Value: strconv.FormatInt(chosenAt, 10)},
			":o": &types.AttributeValueMemberS{Value: owner},
		},
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		return ErrNotOwner
	}
	return err
}

func (x *Dynamo) Unbind(ctx context.Context, thingName, owner string) error {
	// REMOVE only owner, matching marshalDevice: owner is the one attribute a
	// device may lack (that absence is what makes Claim's condition and
	// owner-index sparseness work). name and gameId stay present, reset to
	// their zero values, so unmarshalDevice keeps finding them afterward.
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                aws.String(x.table),
		Key:                      map[string]types.AttributeValue{"thingName": &types.AttributeValueMemberS{Value: thingName}},
		UpdateExpression:         aws.String("REMOVE #o SET #n = :empty, gameId = :zero, chosenAt = :zero, display = :nothing, schedule = :unasked, wake = :empty, sent = :zero"),
		ConditionExpression:      aws.String("#o = :o"),
		ExpressionAttributeNames: map[string]string{"#o": "owner", "#n": "name"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":o":       &types.AttributeValueMemberS{Value: owner},
			":empty":   &types.AttributeValueMemberS{Value: ""},
			":zero":    &types.AttributeValueMemberN{Value: "0"},
			":nothing": &types.AttributeValueMemberS{Value: settings.Stored(settings.Settings{})},
			":unasked": &types.AttributeValueMemberS{Value: schedule.Stored(schedule.Panel{})},
		},
		ReturnValuesOnConditionCheckFailure: types.ReturnValuesOnConditionCheckFailureAllOld,
	})
	var cond *types.ConditionalCheckFailedException
	if errors.As(err, &cond) {
		// The condition fails identically whether thingName doesn't exist or
		// it exists with a different owner; ALL_OLD tells them apart.
		return condFailure(cond.Item, ErrNotFound, ErrNotOwner)
	}
	return err
}
