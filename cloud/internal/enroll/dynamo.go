package enroll

import (
	"context"
	"errors"
	"strconv"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
)

// Dynamo is the Store over DynamoDB. The table carries a TTL on expiresAt, so
// expiry is the database's job rather than a sweeper's -- for the enrollment
// and for its code reservation alike.
type Dynamo struct {
	client *dynamodb.Client
	table  string
}

func NewDynamo(client *dynamodb.Client, table string) *Dynamo {
	return &Dynamo{client: client, table: table}
}

func s(v string) types.AttributeValue { return &types.AttributeValueMemberS{Value: v} }
func n(v int64) types.AttributeValue {
	return &types.AttributeValueMemberN{Value: strconv.FormatInt(v, 10)}
}
func codeKey(codeHash string) string { return "code#" + codeHash }

func (x *Dynamo) enrollmentItem(p Pending) map[string]types.AttributeValue {
	item := map[string]types.AttributeValue{
		"pk":            s(p.TokenHash),
		"codeHash":      s(p.CodeHash),
		"codeExpiresAt": n(p.CodeExpiresAt),
		"csr":           s(p.CSR),
		"thingName":     s(p.ThingName),
		"status":        s(StatusPending),
		"expiresAt":     n(p.ExpiresAt),
	}
	if p.OwnerHintHash != "" {
		item["ownerHintHash"] = s(p.OwnerHintHash)
	}
	return item
}

// reservation is the item that makes a code unique. It expires with the code,
// so a rotated or abandoned code frees itself.
func (x *Dynamo) reservation(codeHash, tokenHash string, expiresAt int64) map[string]types.AttributeValue {
	return map[string]types.AttributeValue{
		"pk":        s(codeKey(codeHash)),
		"tokenHash": s(tokenHash),
		"expiresAt": n(expiresAt),
	}
}

func conditionFailed(err error) bool {
	var single *types.ConditionalCheckFailedException
	if errors.As(err, &single) {
		return true
	}
	var canceled *types.TransactionCanceledException
	if errors.As(err, &canceled) {
		for _, reason := range canceled.CancellationReasons {
			if aws.ToString(reason.Code) == "ConditionalCheckFailed" {
				return true
			}
		}
	}
	return false
}

// reasonFailed reports whether the CancellationReason at index i is a
// ConditionalCheckFailed, without panicking if AWS returned fewer reasons
// than TransactItems -- it is not obliged to return one per item in every
// error shape.
func reasonFailed(reasons []types.CancellationReason, i int) bool {
	return i < len(reasons) && aws.ToString(reasons[i].Code) == "ConditionalCheckFailed"
}

func (x *Dynamo) Create(ctx context.Context, p Pending) error {
	if p.ExpiresAt == 0 {
		return errors.New("enroll: an enrollment needs an expiry")
	}
	_, err := x.client.TransactWriteItems(ctx, &dynamodb.TransactWriteItemsInput{
		TransactItems: []types.TransactWriteItem{
			{Put: &types.Put{
				TableName:           aws.String(x.table),
				Item:                x.enrollmentItem(p),
				ConditionExpression: aws.String("attribute_not_exists(pk)"),
			}},
			{Put: &types.Put{
				TableName:           aws.String(x.table),
				Item:                x.reservation(p.CodeHash, p.TokenHash, p.CodeExpiresAt),
				ConditionExpression: aws.String("attribute_not_exists(pk)"),
			}},
		},
	})
	if err != nil && conditionFailed(err) {
		return ErrCodeTaken
	}
	return err
}

// RotateCode swaps one code for another atomically: the old reservation goes,
// the new one is claimed, and the enrollment points at it. A code already in
// use fails the condition, and the caller generates another.
func (x *Dynamo) RotateCode(ctx context.Context, tokenHash, newCodeHash string, expiresAt int64) error {
	current, found, err := x.ByTokenHash(ctx, tokenHash)
	if err != nil {
		return err
	}
	if !found {
		return ErrNotFound
	}
	_, err = x.client.TransactWriteItems(ctx, &dynamodb.TransactWriteItemsInput{
		TransactItems: []types.TransactWriteItem{
			// Index 0: delete the old reservation. Unconditioned -- it cannot
			// fail a condition check, and the indices below rely on that.
			{Delete: &types.Delete{
				TableName: aws.String(x.table),
				Key:       map[string]types.AttributeValue{"pk": s(codeKey(current.CodeHash))},
			}},
			// Index 1: claim the new reservation. Failing here means the new
			// code collided with someone else's -- the caller generates
			// another and retries.
			{Put: &types.Put{
				TableName:           aws.String(x.table),
				Item:                x.reservation(newCodeHash, tokenHash, expiresAt),
				ConditionExpression: aws.String("attribute_not_exists(pk)"),
			}},
			// Index 2: point the enrollment at the new code, only while it is
			// still pending. Failing here means somebody already claimed this
			// enrollment -- rotating the code would not help, so this must
			// come back as a different error than index 1's, or a caller that
			// retries specifically on ErrCodeTaken (the handler's rotate
			// helper does, five times) would burn attempts for nothing.
			{Update: &types.Update{
				TableName:                aws.String(x.table),
				Key:                      map[string]types.AttributeValue{"pk": s(tokenHash)},
				UpdateExpression:         aws.String("SET codeHash = :c, codeExpiresAt = :e"),
				ConditionExpression:      aws.String("#s = :pending"),
				ExpressionAttributeNames: map[string]string{"#s": "status"},
				ExpressionAttributeValues: map[string]types.AttributeValue{
					":c":       s(newCodeHash),
					":e":       n(expiresAt),
					":pending": s(StatusPending),
				},
			}},
		},
	})
	if err == nil {
		return nil
	}
	// TransactWriteItems reports failures as TransactionCanceledException
	// with one CancellationReason per TransactItem, in submission order.
	// Check index 2 (already claimed) before index 1 (code taken): if both
	// somehow failed at once, "stop, there is nothing to rotate" is the more
	// correct answer than "try another code."
	var canceled *types.TransactionCanceledException
	if errors.As(err, &canceled) {
		if reasonFailed(canceled.CancellationReasons, 2) {
			return ErrAlreadyClaimed
		}
		if reasonFailed(canceled.CancellationReasons, 1) {
			return ErrCodeTaken
		}
	}
	if conditionFailed(err) {
		return ErrCodeTaken
	}
	return err
}

func (x *Dynamo) ByTokenHash(ctx context.Context, tokenHash string) (Pending, bool, error) {
	out, err := x.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName: aws.String(x.table),
		Key:       map[string]types.AttributeValue{"pk": s(tokenHash)},
	})
	if err != nil {
		return Pending{}, false, err
	}
	if out.Item == nil {
		return Pending{}, false, nil
	}
	p, err := unmarshalPending(out.Item)
	return p, err == nil, err
}

// ByCodeHash follows the reservation to the enrollment. No index: the
// reservation item exists to make codes unique, and pointing at its owner is
// free.
func (x *Dynamo) ByCodeHash(ctx context.Context, codeHash string) (Pending, bool, error) {
	out, err := x.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName: aws.String(x.table),
		Key:       map[string]types.AttributeValue{"pk": s(codeKey(codeHash))},
	})
	if err != nil {
		return Pending{}, false, err
	}
	if out.Item == nil {
		return Pending{}, false, nil
	}
	pointer, ok := out.Item["tokenHash"].(*types.AttributeValueMemberS)
	if !ok {
		return Pending{}, false, errors.New("enroll: malformed code reservation")
	}
	return x.ByTokenHash(ctx, pointer.Value)
}

// Reserve is the one-time guard. The condition is what enforces it: an
// enrollment that has left StatusPending fails rather than having its owner
// overwritten. It deliberately runs before any AWS IoT call, so two callers
// racing the same code cannot both mint a certificate.
func (x *Dynamo) Reserve(ctx context.Context, tokenHash, owner string) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:           aws.String(x.table),
		Key:                 map[string]types.AttributeValue{"pk": s(tokenHash)},
		UpdateExpression:    aws.String("SET #s = :claiming, #o = :owner"),
		ConditionExpression: aws.String("attribute_exists(pk) AND #s = :pending"),
		ExpressionAttributeNames: map[string]string{
			"#s": "status", // reserved word
			"#o": "owner",  // reserved word
		},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":claiming": s(StatusClaiming),
			":pending":  s(StatusPending),
			":owner":    s(owner),
		},
		// Lets us tell "no such enrollment" from "already claimed" without a
		// second read, the same way devices.Claim does.
		ReturnValuesOnConditionCheckFailure: types.ReturnValuesOnConditionCheckFailureAllOld,
	})
	if err == nil {
		return nil
	}
	var failed *types.ConditionalCheckFailedException
	if errors.As(err, &failed) {
		if failed.Item == nil {
			return ErrNotFound
		}
		return ErrAlreadyClaimed
	}
	return err
}

// SetCertificate completes what Reserve began. The condition requires
// status = claiming, the same way Reserve requires status = pending: an
// enrollment can only reach ready by way of claiming, so the three-state
// machine is something the database enforces rather than something that
// holds only while callers happen to call Reserve first.
func (x *Dynamo) SetCertificate(ctx context.Context, tokenHash, certPEM string) error {
	_, err := x.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
		TableName:                aws.String(x.table),
		Key:                      map[string]types.AttributeValue{"pk": s(tokenHash)},
		UpdateExpression:         aws.String("SET #s = :ready, certPem = :cert"),
		ConditionExpression:      aws.String("#s = :claiming"),
		ExpressionAttributeNames: map[string]string{"#s": "status"},
		ExpressionAttributeValues: map[string]types.AttributeValue{
			":claiming": s(StatusClaiming),
			":ready":    s(StatusReady),
			":cert":     s(certPEM),
		},
	})
	return err
}

// Delete removes the enrollment and frees its code.
func (x *Dynamo) Delete(ctx context.Context, tokenHash string) error {
	current, found, err := x.ByTokenHash(ctx, tokenHash)
	if err != nil {
		return err
	}
	if !found {
		return nil // already gone; a device may retry a collection that succeeded
	}
	_, err = x.client.TransactWriteItems(ctx, &dynamodb.TransactWriteItemsInput{
		TransactItems: []types.TransactWriteItem{
			{Delete: &types.Delete{
				TableName: aws.String(x.table),
				Key:       map[string]types.AttributeValue{"pk": s(tokenHash)},
			}},
			{Delete: &types.Delete{
				TableName: aws.String(x.table),
				Key:       map[string]types.AttributeValue{"pk": s(codeKey(current.CodeHash))},
			}},
		},
	})
	return err
}

func unmarshalPending(item map[string]types.AttributeValue) (Pending, error) {
	get := func(key string) string {
		if v, ok := item[key].(*types.AttributeValueMemberS); ok {
			return v.Value
		}
		return ""
	}
	num := func(key string) int64 {
		if v, ok := item[key].(*types.AttributeValueMemberN); ok {
			parsed, err := strconv.ParseInt(v.Value, 10, 64)
			if err == nil {
				return parsed
			}
		}
		return 0
	}
	p := Pending{
		TokenHash:     get("pk"),
		CodeHash:      get("codeHash"),
		CodeExpiresAt: num("codeExpiresAt"),
		OwnerHintHash: get("ownerHintHash"),
		CSR:           get("csr"),
		ThingName:     get("thingName"),
		Status:        get("status"),
		CertPEM:       get("certPem"),
		Owner:         get("owner"),
		ExpiresAt:     num("expiresAt"),
	}
	if p.TokenHash == "" || p.ThingName == "" {
		return Pending{}, errors.New("enroll: malformed enrollment row")
	}
	return p, nil
}
