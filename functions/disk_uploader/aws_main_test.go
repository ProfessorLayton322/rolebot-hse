//go:build aws

package main

import (
	"context"
	"io"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

type completionS3 struct {
	put *s3.PutObjectInput
}

func (client *completionS3) DeleteObject(
	context.Context,
	*s3.DeleteObjectInput,
	...func(*s3.Options),
) (*s3.DeleteObjectOutput, error) {
	return &s3.DeleteObjectOutput{}, nil
}

func (client *completionS3) GetObject(
	context.Context,
	*s3.GetObjectInput,
	...func(*s3.Options),
) (*s3.GetObjectOutput, error) {
	return &s3.GetObjectOutput{}, nil
}

func (client *completionS3) PutObject(
	_ context.Context,
	input *s3.PutObjectInput,
	_ ...func(*s3.Options),
) (*s3.PutObjectOutput, error) {
	client.put = input
	return &s3.PutObjectOutput{VersionId: aws.String("version-1")}, nil
}

func TestAWSUploaderRuntimeGateRequiresBothControls(t *testing.T) {
	t.Setenv("APPLICATION_MODE", "standby")
	t.Setenv("MUTATIONS_ENABLED", "false")
	if awsUploaderEnabled() {
		t.Fatal("standby uploader accepted work")
	}
	t.Setenv("APPLICATION_MODE", "active")
	if awsUploaderEnabled() {
		t.Fatal("active uploader accepted work without mutation control")
	}
	t.Setenv("MUTATIONS_ENABLED", "true")
	if !awsUploaderEnabled() {
		t.Fatal("active uploader rejected work")
	}
}

func TestValidateJobAcceptsVersionedAWSStagingKey(t *testing.T) {
	jobID := "01234567-89ab-cdef-0123-456789abcdef"
	job := uploadJob{
		SchemaVersion: 1,
		JobID:         jobID,
		Bucket:        "bucket",
		Files: []uploadFile{{
			Role:          "master",
			ObjectKey:     "uploads/" + jobID + "/master.xlsx",
			ObjectVersion: "version-1",
			DiskPath:      "disk:/larp-bot/events/event-a1.xlsx",
			ContentType:   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
			SHA256:        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			Generation:    "01760000000000000000-" + jobID,
		}},
	}
	if err := validateJob(&job); err != nil {
		t.Fatal(err)
	}
	job.Files[0].ObjectKey = "uploads/another-job/master.xlsx"
	if err := validateJob(&job); err == nil {
		t.Fatal("job accepted an object outside its staging prefix")
	}
}

func TestCompletionIsWrittenOnceForS3Notification(t *testing.T) {
	client := &completionS3{}
	service := awsUploaderService{
		env: awsUploaderEnvironment{Bucket: "private-bucket"},
		s3:  client,
	}
	payload := completion{
		SchemaVersion: 1,
		JobID:         "01234567-89ab-cdef-0123-456789abcdef",
		Success:       true,
		PublicURLs:    map[string]string{"master": "https://example.invalid/master"},
	}
	if err := service.invokeCompletion(context.Background(), payload); err != nil {
		t.Fatal(err)
	}
	if client.put == nil || aws.ToString(client.put.Bucket) != "private-bucket" ||
		aws.ToString(client.put.Key) != "upload-completions/01234567-89ab-cdef-0123-456789abcdef.json" ||
		aws.ToString(client.put.IfNoneMatch) != "*" {
		t.Fatalf("unexpected completion object: %#v", client.put)
	}
	body, err := io.ReadAll(client.put.Body)
	if err != nil {
		t.Fatal(err)
	}
	if len(body) == 0 {
		t.Fatal("completion object is empty")
	}
}
