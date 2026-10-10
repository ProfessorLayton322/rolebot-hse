//go:build aws

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	lambdaruntime "github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	"github.com/aws/smithy-go"
)

type awsUploaderEnvironment struct {
	Bucket          string
	ParameterPrefix string
}

func loadAWSUploaderEnvironment() (awsUploaderEnvironment, error) {
	env := awsUploaderEnvironment{
		Bucket:          os.Getenv("AWS_STORAGE_BUCKET"),
		ParameterPrefix: os.Getenv("AWS_PARAMETER_PREFIX"),
	}
	if env.Bucket == "" || env.ParameterPrefix == "" {
		return awsUploaderEnvironment{}, errors.New("AWS disk uploader environment is incomplete")
	}
	if !strings.HasSuffix(env.ParameterPrefix, "/") {
		env.ParameterPrefix += "/"
	}
	return env, nil
}

func awsUploaderEnabled() bool {
	return os.Getenv("APPLICATION_MODE") == "active" && os.Getenv("MUTATIONS_ENABLED") == "true"
}

type awsS3Client interface {
	DeleteObject(context.Context, *s3.DeleteObjectInput, ...func(*s3.Options)) (*s3.DeleteObjectOutput, error)
	GetObject(context.Context, *s3.GetObjectInput, ...func(*s3.Options)) (*s3.GetObjectOutput, error)
	PutObject(context.Context, *s3.PutObjectInput, ...func(*s3.Options)) (*s3.PutObjectOutput, error)
}

type awsSSMClient interface {
	GetParameter(context.Context, *ssm.GetParameterInput, ...func(*ssm.Options)) (*ssm.GetParameterOutput, error)
}

type awsUploaderService struct {
	env        awsUploaderEnvironment
	s3         awsS3Client
	ssm        awsSSMClient
	disk       *uploaderService
	diskToken  string
	tokenMutex sync.Mutex
}

var (
	defaultAWSUploader   *awsUploaderService
	defaultAWSUploaderMu sync.Mutex
)

func awsUploaderFromEnvironment(ctx context.Context) (*awsUploaderService, error) {
	defaultAWSUploaderMu.Lock()
	defer defaultAWSUploaderMu.Unlock()
	if defaultAWSUploader != nil {
		return defaultAWSUploader, nil
	}
	env, err := loadAWSUploaderEnvironment()
	if err != nil {
		return nil, err
	}
	config, err := awsconfig.LoadDefaultConfig(ctx)
	if err != nil {
		return nil, err
	}
	client := &http.Client{Timeout: 110 * time.Second}
	defaultAWSUploader = &awsUploaderService{
		env: env,
		s3:  s3.NewFromConfig(config),
		ssm: ssm.NewFromConfig(config),
		disk: &uploaderService{
			client:  client,
			diskAPI: yandexDiskAPI,
		},
	}
	return defaultAWSUploader, nil
}

func awsAPIErrorCode(err error) string {
	var apiError smithy.APIError
	if errors.As(err, &apiError) {
		return apiError.ErrorCode()
	}
	return ""
}

func awsObjectMissing(err error) bool {
	code := awsAPIErrorCode(err)
	return code == "NoSuchKey" || code == "NotFound" || code == "NoSuchVersion"
}

func awsObjectConflict(err error) bool {
	code := awsAPIErrorCode(err)
	return code == "PreconditionFailed" || code == "ConditionalRequestConflict"
}

func (service *awsUploaderService) runtimeDiskToken(ctx context.Context) (string, error) {
	service.tokenMutex.Lock()
	defer service.tokenMutex.Unlock()
	if service.diskToken != "" {
		return service.diskToken, nil
	}
	response, err := service.ssm.GetParameter(ctx, &ssm.GetParameterInput{
		Name:           aws.String(service.env.ParameterPrefix + "yandex-disk-token"),
		WithDecryption: aws.Bool(true),
	})
	if err != nil {
		return "", err
	}
	if response.Parameter == nil || response.Parameter.Value == nil || *response.Parameter.Value == "" {
		return "", errors.New("Yandex Disk runtime parameter is empty")
	}
	service.diskToken = *response.Parameter.Value
	return service.diskToken, nil
}

func (service *awsUploaderService) stagedObject(
	ctx context.Context,
	bucket string,
	file uploadFile,
) (*s3.GetObjectOutput, error) {
	response, err := service.s3.GetObject(ctx, &s3.GetObjectInput{
		Bucket:    aws.String(bucket),
		Key:       aws.String(file.ObjectKey),
		VersionId: aws.String(file.ObjectVersion),
	})
	if err != nil {
		if awsObjectMissing(err) {
			return nil, errObjectNotFound
		}
		return nil, err
	}
	if response.Metadata["sha256"] != file.SHA256 || response.Metadata["generation"] != file.Generation {
		response.Body.Close()
		return nil, errors.New("staged S3 object integrity metadata does not match the job")
	}
	return response, nil
}

func (service *awsUploaderService) deleteVersion(
	ctx context.Context,
	bucket string,
	key string,
	version string,
) error {
	input := &s3.DeleteObjectInput{Bucket: aws.String(bucket), Key: aws.String(key)}
	if version != "" {
		input.VersionId = aws.String(version)
	}
	_, err := service.s3.DeleteObject(ctx, input)
	if awsObjectMissing(err) {
		return nil
	}
	return err
}

func (service *awsUploaderService) readLock(
	ctx context.Context,
	bucket string,
	key string,
) (string, string, error) {
	response, err := service.s3.GetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(key),
	})
	if awsObjectMissing(err) {
		return "", "", nil
	}
	if err != nil {
		return "", "", err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 256))
	if err != nil {
		return "", "", err
	}
	version := aws.ToString(response.VersionId)
	return string(body), version, nil
}

func (service *awsUploaderService) acquireDestination(
	ctx context.Context,
	bucket string,
	file uploadFile,
) (string, error) {
	key := lockKey(file.DiskPath)
	for attempt := 0; attempt < 10; attempt++ {
		response, err := service.s3.PutObject(ctx, &s3.PutObjectInput{
			Bucket:      aws.String(bucket),
			Key:         aws.String(key),
			Body:        bytes.NewReader([]byte(file.Generation)),
			ContentType: aws.String("text/plain"),
			IfNoneMatch: aws.String("*"),
		})
		if err == nil {
			version := aws.ToString(response.VersionId)
			if version == "" {
				return "", errors.New("S3 returned no destination lock version")
			}
			return version, nil
		}
		if !awsObjectConflict(err) {
			return "", err
		}
		generation, version, err := service.readLock(ctx, bucket, key)
		if err != nil {
			return "", err
		}
		timestamp, _, found := strings.Cut(generation, "-")
		nanoseconds, parseErr := strconv.ParseInt(timestamp, 10, 64)
		if found && parseErr == nil && version != "" && time.Since(time.Unix(0, nanoseconds)) > lockStaleAfter {
			if err := service.deleteVersion(ctx, bucket, key, version); err != nil {
				return "", err
			}
			continue
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(time.Duration(attempt+1) * 100 * time.Millisecond):
		}
	}
	return "", errDestinationBusy
}

func (service *awsUploaderService) readGeneration(
	ctx context.Context,
	bucket string,
	diskPath string,
) (storedGeneration, error) {
	response, err := service.s3.GetObject(ctx, &s3.GetObjectInput{
		Bucket: aws.String(bucket),
		Key:    aws.String(stateKey(diskPath)),
	})
	if awsObjectMissing(err) {
		return storedGeneration{}, nil
	}
	if err != nil {
		return storedGeneration{}, err
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1024))
	if err != nil {
		return storedGeneration{}, err
	}
	return storedGeneration{Value: string(body), VersionID: aws.ToString(response.VersionId)}, nil
}

func (service *awsUploaderService) writeGeneration(
	ctx context.Context,
	bucket string,
	file uploadFile,
) (string, error) {
	response, err := service.s3.PutObject(ctx, &s3.PutObjectInput{
		Bucket:      aws.String(bucket),
		Key:         aws.String(stateKey(file.DiskPath)),
		Body:        bytes.NewReader([]byte(file.Generation)),
		ContentType: aws.String("text/plain"),
	})
	if err != nil {
		return "", err
	}
	version := aws.ToString(response.VersionId)
	if version == "" {
		return "", errors.New("S3 returned no generation state version")
	}
	return version, nil
}

func (service *awsUploaderService) uploadOne(
	ctx context.Context,
	bucket string,
	file uploadFile,
	diskToken string,
) (string, error) {
	lockVersion, err := service.acquireDestination(ctx, bucket, file)
	if err != nil {
		return "", err
	}
	defer func() {
		if err := service.deleteVersion(ctx, bucket, lockKey(file.DiskPath), lockVersion); err != nil {
			fmt.Printf("release AWS upload lock for role %s: %v\n", file.Role, err)
		}
	}()
	previous, err := service.readGeneration(ctx, bucket, file.DiskPath)
	if err != nil {
		return "", err
	}
	if previous.Value >= file.Generation {
		if err := service.deleteVersion(ctx, bucket, file.ObjectKey, file.ObjectVersion); err != nil {
			return "", err
		}
		return service.disk.existingResult(ctx, file, diskToken)
	}
	staged, err := service.stagedObject(ctx, bucket, file)
	if errors.Is(err, errObjectNotFound) {
		return service.disk.existingResult(ctx, file, diskToken)
	}
	if err != nil {
		return "", err
	}
	defer staged.Body.Close()
	if err := service.disk.ensureDirectories(ctx, file.DiskPath, diskToken); err != nil {
		return "", err
	}
	response, err := service.disk.diskRequest(
		ctx,
		http.MethodGet,
		"resources/upload",
		mapValues(file),
		diskToken,
		nil,
	)
	if err != nil {
		return "", err
	}
	if response.StatusCode == http.StatusConflict {
		response.Body.Close()
		return service.recordExisting(ctx, bucket, file, previous, diskToken)
	}
	body, err := readResponse(response)
	if err != nil {
		return "", err
	}
	var link diskLink
	if err := json.Unmarshal(body, &link); err != nil || !strings.HasPrefix(link.Href, "https://") {
		return "", errors.New("Yandex Disk returned an invalid upload URL")
	}
	uploadRequest, err := http.NewRequestWithContext(ctx, http.MethodPut, link.Href, staged.Body)
	if err != nil {
		return "", err
	}
	uploadRequest.Header.Set("Content-Type", file.ContentType)
	uploadRequest.ContentLength = aws.ToInt64(staged.ContentLength)
	uploadResponse, err := service.disk.client.Do(uploadRequest)
	if err != nil {
		return "", err
	}
	if _, err := readResponse(uploadResponse); err != nil {
		return "", err
	}
	publicURL := ""
	if file.Publish {
		publicURL, err = service.disk.publish(ctx, file.DiskPath, diskToken)
		if err != nil {
			return "", err
		}
	}
	if err := service.finishGeneration(ctx, bucket, file, previous); err != nil {
		return "", err
	}
	return publicURL, nil
}

func mapValues(file uploadFile) map[string][]string {
	return map[string][]string{
		"path":      {file.DiskPath},
		"overwrite": {strconv.FormatBool(file.Overwrite)},
	}
}

func (service *awsUploaderService) recordExisting(
	ctx context.Context,
	bucket string,
	file uploadFile,
	previous storedGeneration,
	diskToken string,
) (string, error) {
	publicURL, err := service.disk.existingResult(ctx, file, diskToken)
	if err != nil {
		return "", err
	}
	if err := service.finishGeneration(ctx, bucket, file, previous); err != nil {
		return "", err
	}
	return publicURL, nil
}

func (service *awsUploaderService) finishGeneration(
	ctx context.Context,
	bucket string,
	file uploadFile,
	previous storedGeneration,
) error {
	newVersion, err := service.writeGeneration(ctx, bucket, file)
	if err != nil {
		return err
	}
	if previous.VersionID != "" {
		if err := service.deleteVersion(ctx, bucket, stateKey(file.DiskPath), previous.VersionID); err != nil {
			fmt.Printf("delete previous S3 generation %s after writing %s: %v\n", previous.VersionID, newVersion, err)
		}
	}
	return service.deleteVersion(ctx, bucket, file.ObjectKey, file.ObjectVersion)
}

func (service *awsUploaderService) invokeCompletion(ctx context.Context, payload completion) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = service.s3.PutObject(ctx, &s3.PutObjectInput{
		Bucket:      aws.String(service.env.Bucket),
		Key:         aws.String("upload-completions/" + payload.JobID + ".json"),
		Body:        bytes.NewReader(body),
		ContentType: aws.String("application/json"),
		IfNoneMatch: aws.String("*"),
	})
	if awsObjectConflict(err) {
		return nil
	}
	return err
}

func (service *awsUploaderService) process(ctx context.Context, job uploadJob) error {
	if job.Bucket != service.env.Bucket {
		return errors.New("upload job uses an unexpected S3 bucket")
	}
	diskToken, err := service.runtimeDiskToken(ctx)
	if err != nil {
		return err
	}
	publicURLs := make(map[string]string)
	for _, file := range job.Files {
		var publicURL string
		for attempt := 0; attempt < 3; attempt++ {
			publicURL, err = service.uploadOne(ctx, job.Bucket, file, diskToken)
			if err == nil {
				break
			}
			time.Sleep(time.Duration(attempt+1) * 100 * time.Millisecond)
		}
		if err != nil {
			if errors.Is(err, errDestinationBusy) || job.Callback == nil {
				return err
			}
			if completionErr := service.invokeCompletion(ctx, completion{
				SchemaVersion: 1,
				JobID:         job.JobID,
				Success:       false,
				PublicURLs:    publicURLs,
				Callback:      job.Callback,
				Error:         err.Error(),
			}); completionErr != nil {
				return completionErr
			}
			for _, stagedFile := range job.Files {
				if cleanupErr := service.deleteVersion(
					ctx,
					job.Bucket,
					stagedFile.ObjectKey,
					stagedFile.ObjectVersion,
				); cleanupErr != nil {
					fmt.Printf("delete staged S3 object after failed job %s: %v\n", job.JobID, cleanupErr)
				}
			}
			return nil
		}
		if file.Publish {
			publicURLs[file.Role] = publicURL
		}
	}
	if job.Callback == nil {
		return nil
	}
	return service.invokeCompletion(ctx, completion{
		SchemaVersion: 1,
		JobID:         job.JobID,
		Success:       true,
		PublicURLs:    publicURLs,
		Callback:      job.Callback,
	})
}

func handleAWSUpload(ctx context.Context, body json.RawMessage) (map[string]any, error) {
	if !awsUploaderEnabled() {
		return map[string]any{"processed": false}, nil
	}
	var job uploadJob
	if err := json.Unmarshal(body, &job); err != nil {
		return nil, fmt.Errorf("decode AWS upload job: %w", err)
	}
	if err := validateJob(&job); err != nil {
		return nil, err
	}
	service, err := awsUploaderFromEnvironment(ctx)
	if err != nil {
		return nil, err
	}
	if err := service.process(ctx, job); err != nil {
		return nil, err
	}
	return map[string]any{"processed": true, "job_id": job.JobID, "uploaded": len(job.Files)}, nil
}

func main() {
	lambdaruntime.Start(handleAWSUpload)
}
