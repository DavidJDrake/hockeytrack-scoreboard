package enroll

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/iot"
)

// stubIoT records every call and can be told to fail at one of them.
type stubIoT struct {
	calls  []string
	failAt string
}

func (s *stubIoT) record(name string) error {
	s.calls = append(s.calls, name)
	if s.failAt == name {
		return errors.New("aws said no")
	}
	return nil
}

func (s *stubIoT) CreateCertificateFromCsr(_ context.Context, in *iot.CreateCertificateFromCsrInput, _ ...func(*iot.Options)) (*iot.CreateCertificateFromCsrOutput, error) {
	if err := s.record("CreateCertificateFromCsr"); err != nil {
		return nil, err
	}
	return &iot.CreateCertificateFromCsrOutput{
		CertificateArn: aws.String("arn:aws:iot:us-east-1:1:cert/abc"),
		CertificateId:  aws.String("abc"),
		CertificatePem: aws.String("CERTPEM"),
	}, nil
}

func (s *stubIoT) CreateThing(_ context.Context, in *iot.CreateThingInput, _ ...func(*iot.Options)) (*iot.CreateThingOutput, error) {
	return &iot.CreateThingOutput{}, s.record("CreateThing")
}

func (s *stubIoT) AttachThingPrincipal(_ context.Context, in *iot.AttachThingPrincipalInput, _ ...func(*iot.Options)) (*iot.AttachThingPrincipalOutput, error) {
	return &iot.AttachThingPrincipalOutput{}, s.record("AttachThingPrincipal")
}

func (s *stubIoT) AttachPolicy(_ context.Context, in *iot.AttachPolicyInput, _ ...func(*iot.Options)) (*iot.AttachPolicyOutput, error) {
	if in.PolicyName == nil || *in.PolicyName != "scoreboard-device" {
		return nil, errors.New("wrong policy")
	}
	return &iot.AttachPolicyOutput{}, s.record("AttachPolicy")
}

func (s *stubIoT) DetachPolicy(_ context.Context, _ *iot.DetachPolicyInput, _ ...func(*iot.Options)) (*iot.DetachPolicyOutput, error) {
	return &iot.DetachPolicyOutput{}, s.record("DetachPolicy")
}

func (s *stubIoT) DetachThingPrincipal(_ context.Context, _ *iot.DetachThingPrincipalInput, _ ...func(*iot.Options)) (*iot.DetachThingPrincipalOutput, error) {
	return &iot.DetachThingPrincipalOutput{}, s.record("DetachThingPrincipal")
}

func (s *stubIoT) DeleteThing(_ context.Context, _ *iot.DeleteThingInput, _ ...func(*iot.Options)) (*iot.DeleteThingOutput, error) {
	return &iot.DeleteThingOutput{}, s.record("DeleteThing")
}

func (s *stubIoT) UpdateCertificate(_ context.Context, _ *iot.UpdateCertificateInput, _ ...func(*iot.Options)) (*iot.UpdateCertificateOutput, error) {
	return &iot.UpdateCertificateOutput{}, s.record("UpdateCertificate")
}

func (s *stubIoT) DeleteCertificate(_ context.Context, _ *iot.DeleteCertificateInput, _ ...func(*iot.Options)) (*iot.DeleteCertificateOutput, error) {
	return &iot.DeleteCertificateOutput{}, s.record("DeleteCertificate")
}

func TestIssueMakesTheFourCallsInOrder(t *testing.T) {
	stub := &stubIoT{}
	pem, err := NewIoTIssuer(stub, "scoreboard-device").Issue(context.Background(), "CSR", "scoreboard-abc")
	if err != nil {
		t.Fatal(err)
	}
	if pem != "CERTPEM" {
		t.Errorf("got certificate %q", pem)
	}
	want := "CreateCertificateFromCsr,CreateThing,AttachThingPrincipal,AttachPolicy"
	if got := strings.Join(stub.calls, ","); got != want {
		t.Errorf("calls were %q, want %q", got, want)
	}
}

func TestIssueCleansUpWhenAStepFails(t *testing.T) {
	// An orphaned certificate or a thing with no policy is a credential
	// nobody can account for. Every partial failure must unwind.
	for _, tc := range []struct {
		failAt  string
		cleanup []string
	}{
		{"CreateThing", []string{"UpdateCertificate", "DeleteCertificate"}},
		{"AttachThingPrincipal", []string{"DeleteThing", "UpdateCertificate", "DeleteCertificate"}},
		{"AttachPolicy", []string{"DetachThingPrincipal", "DeleteThing", "UpdateCertificate", "DeleteCertificate"}},
	} {
		t.Run(tc.failAt, func(t *testing.T) {
			stub := &stubIoT{failAt: tc.failAt}
			_, err := NewIoTIssuer(stub, "scoreboard-device").Issue(context.Background(), "CSR", "scoreboard-abc")
			if err == nil {
				t.Fatal("expected an error")
			}
			for _, want := range tc.cleanup {
				found := false
				for _, call := range stub.calls {
					if call == want {
						found = true
					}
				}
				if !found {
					t.Errorf("failure at %s did not call %s; calls were %v", tc.failAt, want, stub.calls)
				}
			}
		})
	}
}

func TestIssueFailsBeforeAnythingExistsWhenSigningFails(t *testing.T) {
	stub := &stubIoT{failAt: "CreateCertificateFromCsr"}
	if _, err := NewIoTIssuer(stub, "scoreboard-device").Issue(context.Background(), "CSR", "scoreboard-abc"); err == nil {
		t.Fatal("expected an error")
	}
	if len(stub.calls) != 1 {
		t.Errorf("nothing should be cleaned up when nothing was created; calls were %v", stub.calls)
	}
}

func TestFakeIssuerRecordsWhatItWasAsked(t *testing.T) {
	f := &FakeIssuer{CertPEM: "PEM"}
	got, err := f.Issue(context.Background(), "CSR", "scoreboard-xyz")
	if err != nil || got != "PEM" {
		t.Fatalf("got %q, %v", got, err)
	}
	if len(f.Calls) != 1 || f.Calls[0] != "scoreboard-xyz" {
		t.Errorf("calls were %v", f.Calls)
	}
}
