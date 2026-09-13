package enroll

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iot"
	"github.com/aws/aws-sdk-go-v2/service/iot/types"
)

// Issuer turns a validated certificate signing request into a working device
// identity. It is a port so that nothing but IoTIssuer needs AWS, and so the
// handler's tests can run without it.
type Issuer interface {
	Issue(ctx context.Context, csrPEM, thingName string) (certPEM string, err error)
}

// IoTAPI is the slice of the AWS IoT control plane this needs. Narrow on
// purpose: the interface is the list of things this component is allowed to do.
type IoTAPI interface {
	CreateCertificateFromCsr(context.Context, *iot.CreateCertificateFromCsrInput, ...func(*iot.Options)) (*iot.CreateCertificateFromCsrOutput, error)
	CreateThing(context.Context, *iot.CreateThingInput, ...func(*iot.Options)) (*iot.CreateThingOutput, error)
	AttachThingPrincipal(context.Context, *iot.AttachThingPrincipalInput, ...func(*iot.Options)) (*iot.AttachThingPrincipalOutput, error)
	AttachPolicy(context.Context, *iot.AttachPolicyInput, ...func(*iot.Options)) (*iot.AttachPolicyOutput, error)
	DetachPolicy(context.Context, *iot.DetachPolicyInput, ...func(*iot.Options)) (*iot.DetachPolicyOutput, error)
	DetachThingPrincipal(context.Context, *iot.DetachThingPrincipalInput, ...func(*iot.Options)) (*iot.DetachThingPrincipalOutput, error)
	DeleteThing(context.Context, *iot.DeleteThingInput, ...func(*iot.Options)) (*iot.DeleteThingOutput, error)
	UpdateCertificate(context.Context, *iot.UpdateCertificateInput, ...func(*iot.Options)) (*iot.UpdateCertificateOutput, error)
	DeleteCertificate(context.Context, *iot.DeleteCertificateInput, ...func(*iot.Options)) (*iot.DeleteCertificateOutput, error)
}

// IoTIssuer mints device identities in AWS IoT.
//
// The policy name is fixed at construction rather than passed per call, so no
// caller can choose which policy a new certificate receives. The IAM role this
// runs under must also pin iot:AttachPolicy to that one policy ARN; without
// that second constraint, a bug in device onboarding here becomes compromise
// of the whole fleet.
type IoTIssuer struct {
	api        IoTAPI
	policyName string
}

func NewIoTIssuer(api IoTAPI, policyName string) *IoTIssuer {
	return &IoTIssuer{api: api, policyName: policyName}
}

func (i *IoTIssuer) Issue(ctx context.Context, csrPEM, thingName string) (string, error) {
	cert, err := i.api.CreateCertificateFromCsr(ctx, &iot.CreateCertificateFromCsrInput{
		CertificateSigningRequest: aws.String(csrPEM),
		SetAsActive:               true,
	})
	if err != nil {
		// Nothing was created, so there is nothing to unwind.
		return "", fmt.Errorf("signing the certificate request: %w", err)
	}
	certARN, certID := aws.ToString(cert.CertificateArn), aws.ToString(cert.CertificateId)

	if _, err := i.api.CreateThing(ctx, &iot.CreateThingInput{ThingName: aws.String(thingName)}); err != nil {
		i.rollback(ctx, thingName, certARN, certID, false, false)
		return "", fmt.Errorf("creating the thing: %w", err)
	}
	if _, err := i.api.AttachThingPrincipal(ctx, &iot.AttachThingPrincipalInput{
		ThingName: aws.String(thingName),
		Principal: aws.String(certARN),
	}); err != nil {
		i.rollback(ctx, thingName, certARN, certID, true, false)
		return "", fmt.Errorf("attaching the certificate to the thing: %w", err)
	}
	if _, err := i.api.AttachPolicy(ctx, &iot.AttachPolicyInput{
		PolicyName: aws.String(i.policyName),
		Target:     aws.String(certARN),
	}); err != nil {
		i.rollback(ctx, thingName, certARN, certID, true, true)
		return "", fmt.Errorf("attaching the device policy: %w", err)
	}
	return aws.ToString(cert.CertificatePem), nil
}

// rollback undoes as much as was done, in reverse. Best effort: a cleanup
// failure is logged and does not mask the original error, because the caller
// needs to know the enrollment failed more than it needs to know why the
// tidying afterwards also failed.
func (i *IoTIssuer) rollback(ctx context.Context, thingName, certARN, certID string, thingExists, principalAttached bool) {
	if principalAttached {
		if _, err := i.api.DetachThingPrincipal(ctx, &iot.DetachThingPrincipalInput{
			ThingName: aws.String(thingName),
			Principal: aws.String(certARN),
		}); err != nil {
			slog.Error("enroll rollback: detaching principal", "thing", thingName, "err", err)
		}
	}
	if thingExists {
		if _, err := i.api.DeleteThing(ctx, &iot.DeleteThingInput{ThingName: aws.String(thingName)}); err != nil {
			slog.Error("enroll rollback: deleting thing", "thing", thingName, "err", err)
		}
	}
	// A certificate has to be INACTIVE before it can be deleted.
	if _, err := i.api.UpdateCertificate(ctx, &iot.UpdateCertificateInput{
		CertificateId: aws.String(certID),
		NewStatus:     types.CertificateStatusInactive,
	}); err != nil {
		// Deactivation itself failed, so the certificate is still ACTIVE and
		// now orphaned: AWS will refuse to delete an ACTIVE certificate, so
		// there is no point trying. It carries no policy and no principal --
		// AttachPolicy is the last of the four calls, so no rollback path is
		// ever reached with a policy attached -- so it cannot be used to
		// connect or publish. It is inert, but it will not clean itself up;
		// someone has to remove it by hand.
		slog.Error("enroll rollback: certificate left ACTIVE and orphaned; deactivation failed so deletion was not attempted", "certificate", certID, "err", err)
		return
	}
	if _, err := i.api.DeleteCertificate(ctx, &iot.DeleteCertificateInput{
		CertificateId: aws.String(certID),
	}); err != nil {
		slog.Error("enroll rollback: deleting certificate", "certificate", certID, "err", err)
	}
}

// FakeIssuer is the Issuer the handler's tests use.
type FakeIssuer struct {
	CertPEM string
	Err     error
	Calls   []string // thing names it was asked to issue for
}

func (f *FakeIssuer) Issue(_ context.Context, _, thingName string) (string, error) {
	f.Calls = append(f.Calls, thingName)
	if f.Err != nil {
		return "", f.Err
	}
	return f.CertPEM, nil
}
