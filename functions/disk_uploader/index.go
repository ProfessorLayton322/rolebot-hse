package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	metadataTokenURL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
	tokenExchangeURL = "https://auth.yandex.cloud/oauth/token"
	yandexDiskAPI    = "https://cloud-api.yandex.net/v1/disk"
	objectStorageURL = "https://storage.yandexcloud.net"
	stagingPrefix    = "larp-bot/uploads/"
	statePrefix      = "larp-bot/upload-state/"
	lockPrefix       = "larp-bot/upload-locks/"
	lockStaleAfter   = 6 * time.Minute
)

var (
	jobIDPattern       = regexp.MustCompile(`^[0-9a-f-]{36}$`)
	rolePattern        = regexp.MustCompile(`^[a-z][a-z0-9_-]{0,31}$`)
	digestPattern      = regexp.MustCompile(`^[0-9a-f]{64}$`)
	generationPattern  = regexp.MustCompile(`^[0-9]{20}-[0-9a-f-]{36}$`)
	defaultService     *uploaderService
	defaultServiceMu   sync.Mutex
	errObjectNotFound  = errors.New("object not found")
	errDestinationBusy = errors.New("yandex disk destination is busy")
)

type uploadFile struct {
	Role          string `json:"role"`
	ObjectKey     string `json:"object_key"`
	ObjectVersion string `json:"object_version,omitempty"`
	DiskPath      string `json:"disk_path"`
	ContentType   string `json:"content_type"`
	SHA256        string `json:"sha256"`
	Generation    string `json:"generation"`
	Overwrite     bool   `json:"overwrite"`
	Publish       bool   `json:"publish"`
}

type storedGeneration struct {
	Value     string
	VersionID string
}

type uploadJob struct {
	SchemaVersion int            `json:"schema_version"`
	JobID         string         `json:"job_id"`
	Bucket        string         `json:"bucket"`
	Files         []uploadFile   `json:"files"`
	Callback      map[string]any `json:"callback"`
}

type completion struct {
	SchemaVersion int               `json:"schema_version"`
	JobID         string            `json:"job_id"`
	Success       bool              `json:"success"`
	PublicURLs    map[string]string `json:"public_urls"`
	Callback      map[string]any    `json:"callback"`
	Error         string            `json:"error,omitempty"`
}

type environment struct {
	Bucket                  string
	RuntimeConfigURL        string
	RuntimeConfigAudience   string
	RuntimeServiceAccountID string
	CompletionFunctionURL   string
}

type uploaderService struct {
	env        environment
	client     *http.Client
	metadata   string
	tokenURL   string
	diskAPI    string
	storageURL string
}

type metadataToken struct {
	AccessToken string `json:"access_token"`
}

type exchangedToken struct {
	AccessToken string `json:"access_token"`
	IDToken     string `json:"id_token"`
}

type runtimeConfig struct {
	Values map[string]string `json:"values"`
}

type diskLink struct {
	Href string `json:"href"`
}

type diskResource struct {
	PublicURL string `json:"public_url"`
}

type httpStatusError struct {
	StatusCode int
	Method     string
	URL        string
	Body       string
}

func (e *httpStatusError) Error() string {
	return fmt.Sprintf("%s %s returned HTTP %d: %s", e.Method, e.URL, e.StatusCode, e.Body)
}

func loadEnvironment() (environment, error) {
	env := environment{
		Bucket:                  os.Getenv("DISK_STAGING_BUCKET"),
		RuntimeConfigURL:        os.Getenv("RUNTIME_CONFIG_URL"),
		RuntimeConfigAudience:   os.Getenv("RUNTIME_CONFIG_AUDIENCE"),
		RuntimeServiceAccountID: os.Getenv("RUNTIME_SERVICE_ACCOUNT_ID"),
		CompletionFunctionURL:   os.Getenv("UPLOAD_COMPLETION_URL"),
	}
	if env.Bucket == "" || env.RuntimeConfigURL == "" || env.RuntimeConfigAudience == "" ||
		env.RuntimeServiceAccountID == "" || env.CompletionFunctionURL == "" {
		return environment{}, errors.New("uploader environment is incomplete")
	}
	return env, nil
}

func serviceFromEnvironment() (*uploaderService, error) {
	defaultServiceMu.Lock()
	defer defaultServiceMu.Unlock()
	if defaultService != nil {
		return defaultService, nil
	}
	env, err := loadEnvironment()
	if err != nil {
		return nil, err
	}
	defaultService = &uploaderService{
		env:        env,
		client:     &http.Client{Timeout: 110 * time.Second},
		metadata:   metadataTokenURL,
		tokenURL:   tokenExchangeURL,
		diskAPI:    yandexDiskAPI,
		storageURL: objectStorageURL,
	}
	return defaultService, nil
}

func validateJob(job *uploadJob) error {
	if job.SchemaVersion != 1 || !jobIDPattern.MatchString(job.JobID) {
		return errors.New("invalid upload job schema or ID")
	}
	if job.Bucket == "" || len(job.Files) == 0 || len(job.Files) > 8 {
		return errors.New("upload job has an invalid bucket or file count")
	}
	seen := make(map[string]bool, len(job.Files))
	for _, file := range job.Files {
		if !rolePattern.MatchString(file.Role) || seen[file.Role] {
			return errors.New("upload file roles must be valid and unique")
		}
		seen[file.Role] = true
		if !strings.HasPrefix(file.ObjectKey, stagingPrefix+job.JobID+"/") || strings.Contains(file.ObjectKey, "..") {
			return errors.New("upload object key is outside the job staging directory")
		}
		if len(file.ObjectVersion) > 1024 || strings.ContainsAny(file.ObjectVersion, "\r\n") {
			return errors.New("upload object version is invalid")
		}
		if !strings.HasPrefix(file.DiskPath, "disk:/larp-bot/") || strings.Contains(file.DiskPath, "..") ||
			strings.ContainsAny(file.DiskPath, "\r\n") {
			return errors.New("invalid Yandex Disk destination")
		}
		if !digestPattern.MatchString(file.SHA256) || !generationPattern.MatchString(file.Generation) {
			return errors.New("upload file has invalid integrity metadata")
		}
		switch file.ContentType {
		case "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
			"application/vnd.openxmlformats-officedocument.presentationml.presentation":
		default:
			return errors.New("upload file has an unsupported content type")
		}
	}
	return nil
}

func readResponse(response *http.Response) ([]byte, error) {
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, &httpStatusError{
			StatusCode: response.StatusCode,
			Method:     response.Request.Method,
			URL:        response.Request.URL.String(),
			Body:       string(body),
		}
	}
	return body, nil
}

func (service *uploaderService) iamToken(ctx context.Context) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, service.metadata, nil)
	if err != nil {
		return "", err
	}
	request.Header.Set("Metadata-Flavor", "Google")
	response, err := service.client.Do(request)
	if err != nil {
		return "", err
	}
	body, err := readResponse(response)
	if err != nil {
		return "", err
	}
	var token metadataToken
	if err := json.Unmarshal(body, &token); err != nil || token.AccessToken == "" {
		return "", errors.New("metadata service returned no IAM token")
	}
	return token.AccessToken, nil
}

func (service *uploaderService) diskToken(ctx context.Context, iamToken string) (string, error) {
	values := url.Values{
		"actor_token":          {iamToken},
		"actor_token_type":     {"urn:ietf:params:oauth:token-type:access_token"},
		"audience":             {service.env.RuntimeConfigAudience},
		"grant_type":           {"urn:ietf:params:oauth:grant-type:token-exchange"},
		"requested_token_type": {"urn:ietf:params:oauth:token-type:id_token"},
		"subject_token":        {service.env.RuntimeServiceAccountID},
		"subject_token_type":   {"urn:yandex-cloud:token-type:subject_id"},
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, service.tokenURL, strings.NewReader(values.Encode()))
	if err != nil {
		return "", err
	}
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response, err := service.client.Do(request)
	if err != nil {
		return "", err
	}
	body, err := readResponse(response)
	if err != nil {
		return "", err
	}
	var exchanged exchangedToken
	if err := json.Unmarshal(body, &exchanged); err != nil {
		return "", err
	}
	identityToken := exchanged.AccessToken
	if identityToken == "" {
		identityToken = exchanged.IDToken
	}
	if identityToken == "" {
		return "", errors.New("identity exchange returned no token")
	}
	request, err = http.NewRequestWithContext(ctx, http.MethodGet, service.env.RuntimeConfigURL, nil)
	if err != nil {
		return "", err
	}
	request.Header.Set("Authorization", "Bearer "+identityToken)
	response, err = service.client.Do(request)
	if err != nil {
		return "", err
	}
	body, err = readResponse(response)
	if err != nil {
		return "", err
	}
	var config runtimeConfig
	if err := json.Unmarshal(body, &config); err != nil {
		return "", err
	}
	token := config.Values["YANDEX_DISK_TOKEN"]
	if token == "" {
		return "", errors.New("runtime configuration has no Yandex Disk token")
	}
	return token, nil
}

func escapeObjectPath(value string) string {
	parts := strings.Split(value, "/")
	for index := range parts {
		parts[index] = url.PathEscape(parts[index])
	}
	return strings.Join(parts, "/")
}

func (service *uploaderService) objectURL(bucket string, key string) string {
	return service.storageURL + "/" + url.PathEscape(bucket) + "/" + escapeObjectPath(key)
}

func (service *uploaderService) objectRequest(
	ctx context.Context,
	method string,
	bucket string,
	key string,
	iamToken string,
	body io.Reader,
) (*http.Response, error) {
	request, err := http.NewRequestWithContext(ctx, method, service.objectURL(bucket, key), body)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+iamToken)
	return service.client.Do(request)
}

func (service *uploaderService) getObject(
	ctx context.Context,
	bucket string,
	file uploadFile,
	iamToken string,
) (*http.Response, error) {
	response, err := service.objectRequest(ctx, http.MethodGet, bucket, file.ObjectKey, iamToken, nil)
	if err != nil {
		return nil, err
	}
	if response.StatusCode == http.StatusNotFound {
		response.Body.Close()
		return nil, errObjectNotFound
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		_, statusErr := readResponse(response)
		return nil, statusErr
	}
	if response.Header.Get("X-Amz-Meta-Sha256") != file.SHA256 ||
		response.Header.Get("X-Amz-Meta-Generation") != file.Generation {
		response.Body.Close()
		return nil, errors.New("staged object integrity metadata does not match the job")
	}
	return response, nil
}

func stateKey(diskPath string) string {
	digest := sha256.Sum256([]byte(diskPath))
	return statePrefix + hex.EncodeToString(digest[:]) + ".txt"
}

func lockKey(diskPath string) string {
	digest := sha256.Sum256([]byte(diskPath))
	return lockPrefix + hex.EncodeToString(digest[:]) + ".lock"
}

func (service *uploaderService) takeOverStaleDestinationLock(
	ctx context.Context,
	bucket string,
	file uploadFile,
	iamToken string,
) (string, bool, error) {
	key := lockKey(file.DiskPath)
	response, err := service.objectRequest(ctx, http.MethodGet, bucket, key, iamToken, nil)
	if err != nil {
		return "", false, err
	}
	if response.StatusCode == http.StatusNotFound {
		response.Body.Close()
		return "", false, nil
	}
	etag := response.Header.Get("ETag")
	staleVersion := response.Header.Get("X-Amz-Version-Id")
	body, err := readResponse(response)
	if err != nil {
		return "", false, err
	}
	timestamp, _, found := strings.Cut(string(body), "-")
	if !found || etag == "" {
		return "", false, nil
	}
	nanoseconds, err := strconv.ParseInt(timestamp, 10, 64)
	if err != nil || time.Since(time.Unix(0, nanoseconds)) <= lockStaleAfter {
		return "", false, nil
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPut,
		service.objectURL(bucket, key),
		strings.NewReader(file.Generation),
	)
	if err != nil {
		return "", false, err
	}
	request.Header.Set("Authorization", "Bearer "+iamToken)
	request.Header.Set("If-Match", etag)
	response, err = service.client.Do(request)
	if err != nil {
		return "", false, err
	}
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		versionID := response.Header.Get("X-Amz-Version-Id")
		response.Body.Close()
		if versionID == "" {
			return "", false, errors.New("Object Storage returned no lock version")
		}
		if staleVersion != "" {
			if err := service.deleteObjectVersion(ctx, bucket, key, staleVersion, iamToken); err != nil {
				return "", false, fmt.Errorf("delete stale lock version: %w", err)
			}
		}
		return versionID, true, nil
	}
	if response.StatusCode == http.StatusConflict || response.StatusCode == http.StatusPreconditionFailed {
		response.Body.Close()
		return "", false, nil
	}
	_, statusErr := readResponse(response)
	return "", false, statusErr
}

func (service *uploaderService) acquireDestination(
	ctx context.Context,
	bucket string,
	file uploadFile,
	iamToken string,
) (string, error) {
	for attempt := 0; attempt < 10; attempt++ {
		request, err := http.NewRequestWithContext(
			ctx,
			http.MethodPut,
			service.objectURL(bucket, lockKey(file.DiskPath)),
			strings.NewReader(file.Generation),
		)
		if err != nil {
			return "", err
		}
		request.Header.Set("Authorization", "Bearer "+iamToken)
		request.Header.Set("If-None-Match", "*")
		response, err := service.client.Do(request)
		if err != nil {
			return "", err
		}
		if response.StatusCode >= 200 && response.StatusCode < 300 {
			versionID := response.Header.Get("X-Amz-Version-Id")
			response.Body.Close()
			if versionID == "" {
				return "", errors.New("Object Storage returned no lock version")
			}
			return versionID, nil
		}
		if response.StatusCode != http.StatusConflict && response.StatusCode != http.StatusPreconditionFailed {
			_, statusErr := readResponse(response)
			return "", statusErr
		}
		response.Body.Close()
		versionID, takenOver, err := service.takeOverStaleDestinationLock(ctx, bucket, file, iamToken)
		if err != nil {
			return "", err
		}
		if takenOver {
			return versionID, nil
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-time.After(time.Duration(attempt+1) * 100 * time.Millisecond):
		}
	}
	return "", errDestinationBusy
}

func (service *uploaderService) deleteObjectVersion(
	ctx context.Context,
	bucket string,
	key string,
	versionID string,
	iamToken string,
) error {
	requestURL := service.objectURL(bucket, key)
	if versionID != "" {
		requestURL += "?" + url.Values{"versionId": {versionID}}.Encode()
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodDelete, requestURL, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+iamToken)
	response, err := service.client.Do(request)
	if err != nil {
		return err
	}
	if response.StatusCode == http.StatusNotFound || response.StatusCode == http.StatusNoContent {
		response.Body.Close()
		return nil
	}
	_, err = readResponse(response)
	return err
}

func (service *uploaderService) releaseDestination(
	ctx context.Context,
	bucket string,
	diskPath string,
	versionID string,
	iamToken string,
) error {
	return service.deleteObjectVersion(ctx, bucket, lockKey(diskPath), versionID, iamToken)
}

func (service *uploaderService) readGeneration(
	ctx context.Context,
	bucket string,
	diskPath string,
	iamToken string,
) (storedGeneration, error) {
	response, err := service.objectRequest(ctx, http.MethodGet, bucket, stateKey(diskPath), iamToken, nil)
	if err != nil {
		return storedGeneration{}, err
	}
	if response.StatusCode == http.StatusNotFound {
		response.Body.Close()
		return storedGeneration{}, nil
	}
	versionID := response.Header.Get("X-Amz-Version-Id")
	body, err := readResponse(response)
	if err != nil {
		return storedGeneration{}, err
	}
	return storedGeneration{Value: string(body), VersionID: versionID}, nil
}

func (service *uploaderService) writeGeneration(
	ctx context.Context,
	bucket string,
	file uploadFile,
	iamToken string,
) (string, error) {
	response, err := service.objectRequest(
		ctx,
		http.MethodPut,
		bucket,
		stateKey(file.DiskPath),
		iamToken,
		strings.NewReader(file.Generation),
	)
	if err != nil {
		return "", err
	}
	versionID := response.Header.Get("X-Amz-Version-Id")
	_, err = readResponse(response)
	if err != nil {
		return "", err
	}
	if versionID == "" {
		return "", errors.New("Object Storage returned no generation version")
	}
	return versionID, nil
}

func (service *uploaderService) deleteStaged(
	ctx context.Context,
	bucket string,
	file uploadFile,
	iamToken string,
) error {
	return service.deleteObjectVersion(ctx, bucket, file.ObjectKey, file.ObjectVersion, iamToken)
}

func (service *uploaderService) diskRequest(
	ctx context.Context,
	method string,
	endpoint string,
	query url.Values,
	token string,
	body io.Reader,
) (*http.Response, error) {
	requestURL := service.diskAPI + "/" + endpoint
	if len(query) > 0 {
		requestURL += "?" + query.Encode()
	}
	request, err := http.NewRequestWithContext(ctx, method, requestURL, body)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "OAuth "+token)
	return service.client.Do(request)
}

func (service *uploaderService) ensureDirectories(ctx context.Context, diskPath string, token string) error {
	parent := path.Dir(strings.TrimPrefix(diskPath, "disk:/"))
	parts := strings.Split(parent, "/")
	for index := range parts {
		directory := "disk:/" + strings.Join(parts[:index+1], "/")
		response, err := service.diskRequest(
			ctx,
			http.MethodPut,
			"resources",
			url.Values{"path": {directory}},
			token,
			nil,
		)
		if err != nil {
			return err
		}
		if response.StatusCode == http.StatusConflict {
			response.Body.Close()
			continue
		}
		if _, err := readResponse(response); err != nil {
			return err
		}
	}
	return nil
}

func (service *uploaderService) resource(ctx context.Context, diskPath string, token string) (*diskResource, error) {
	response, err := service.diskRequest(
		ctx,
		http.MethodGet,
		"resources",
		url.Values{"path": {diskPath}, "fields": {"public_url"}},
		token,
		nil,
	)
	if err != nil {
		return nil, err
	}
	if response.StatusCode == http.StatusNotFound {
		response.Body.Close()
		return nil, errObjectNotFound
	}
	body, err := readResponse(response)
	if err != nil {
		return nil, err
	}
	var resource diskResource
	if err := json.Unmarshal(body, &resource); err != nil {
		return nil, err
	}
	return &resource, nil
}

func (service *uploaderService) publish(ctx context.Context, diskPath string, token string) (string, error) {
	response, err := service.diskRequest(
		ctx,
		http.MethodPut,
		"resources/publish",
		url.Values{"path": {diskPath}},
		token,
		nil,
	)
	if err != nil {
		return "", err
	}
	if _, err := readResponse(response); err != nil {
		return "", err
	}
	resource, err := service.resource(ctx, diskPath, token)
	if err != nil {
		return "", err
	}
	if !strings.HasPrefix(resource.PublicURL, "https://") {
		return "", errors.New("published Yandex Disk resource has no public URL")
	}
	return resource.PublicURL, nil
}

func (service *uploaderService) existingResult(
	ctx context.Context,
	file uploadFile,
	token string,
) (string, error) {
	resource, err := service.resource(ctx, file.DiskPath, token)
	if err != nil {
		return "", err
	}
	if !file.Publish {
		return "", nil
	}
	if strings.HasPrefix(resource.PublicURL, "https://") {
		return resource.PublicURL, nil
	}
	return service.publish(ctx, file.DiskPath, token)
}

func (service *uploaderService) uploadOne(
	ctx context.Context,
	bucket string,
	file uploadFile,
	iamToken string,
	diskToken string,
) (string, error) {
	lockVersion, err := service.acquireDestination(ctx, bucket, file, iamToken)
	if err != nil {
		return "", err
	}
	defer func() {
		if err := service.releaseDestination(ctx, bucket, file.DiskPath, lockVersion, iamToken); err != nil {
			fmt.Printf("release upload lock for role %s: %v\n", file.Role, err)
		}
	}()
	previous, err := service.readGeneration(ctx, bucket, file.DiskPath, iamToken)
	if err != nil {
		return "", err
	}
	if previous.Value >= file.Generation {
		if err := service.deleteStaged(ctx, bucket, file, iamToken); err != nil {
			return "", err
		}
		return service.existingResult(ctx, file, diskToken)
	}
	staged, err := service.getObject(ctx, bucket, file, iamToken)
	if errors.Is(err, errObjectNotFound) {
		return service.existingResult(ctx, file, diskToken)
	}
	if err != nil {
		return "", err
	}
	defer staged.Body.Close()
	if err := service.ensureDirectories(ctx, file.DiskPath, diskToken); err != nil {
		return "", err
	}
	response, err := service.diskRequest(
		ctx,
		http.MethodGet,
		"resources/upload",
		url.Values{
			"path":      {file.DiskPath},
			"overwrite": {fmt.Sprintf("%t", file.Overwrite)},
		},
		diskToken,
		nil,
	)
	if err != nil {
		return "", err
	}
	if response.StatusCode == http.StatusConflict {
		response.Body.Close()
		publicURL, existingErr := service.existingResult(ctx, file, diskToken)
		if existingErr != nil {
			return "", existingErr
		}
		generationVersion, err := service.writeGeneration(ctx, bucket, file, iamToken)
		if err != nil {
			return "", err
		}
		if previous.VersionID != "" {
			if err := service.deleteObjectVersion(
				ctx,
				bucket,
				stateKey(file.DiskPath),
				previous.VersionID,
				iamToken,
			); err != nil {
				fmt.Printf("delete previous generation %s after writing %s: %v\n", previous.VersionID, generationVersion, err)
			}
		}
		if err := service.deleteStaged(ctx, bucket, file, iamToken); err != nil {
			return "", err
		}
		return publicURL, nil
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
	uploadRequest.ContentLength = staged.ContentLength
	uploadResponse, err := service.client.Do(uploadRequest)
	if err != nil {
		return "", err
	}
	if _, err := readResponse(uploadResponse); err != nil {
		return "", err
	}
	publicURL := ""
	if file.Publish {
		publicURL, err = service.publish(ctx, file.DiskPath, diskToken)
		if err != nil {
			return "", err
		}
	}
	generationVersion, err := service.writeGeneration(ctx, bucket, file, iamToken)
	if err != nil {
		return "", err
	}
	if previous.VersionID != "" {
		if err := service.deleteObjectVersion(
			ctx,
			bucket,
			stateKey(file.DiskPath),
			previous.VersionID,
			iamToken,
		); err != nil {
			fmt.Printf("delete previous generation %s after writing %s: %v\n", previous.VersionID, generationVersion, err)
		}
	}
	if err := service.deleteStaged(ctx, bucket, file, iamToken); err != nil {
		return "", err
	}
	return publicURL, nil
}

func (service *uploaderService) invokeCompletion(
	ctx context.Context,
	iamToken string,
	payload completion,
) error {
	body, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	completionURL := strings.TrimRight(service.env.CompletionFunctionURL, "/") + "?integration=async"
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, completionURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+iamToken)
	request.Header.Set("Content-Type", "application/json")
	response, err := service.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusAccepted {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return fmt.Errorf("completion function returned HTTP %d: %s", response.StatusCode, body)
	}
	return nil
}

func (service *uploaderService) process(ctx context.Context, job uploadJob) error {
	if job.Bucket != service.env.Bucket {
		return errors.New("upload job uses an unexpected bucket")
	}
	iamToken, err := service.iamToken(ctx)
	if err != nil {
		return err
	}
	diskToken, err := service.diskToken(ctx, iamToken)
	if err != nil {
		return err
	}
	publicURLs := make(map[string]string)
	for _, file := range job.Files {
		var publicURL string
		for attempt := 0; attempt < 3; attempt++ {
			publicURL, err = service.uploadOne(ctx, job.Bucket, file, iamToken, diskToken)
			if err == nil {
				break
			}
			time.Sleep(time.Duration(attempt+1) * 100 * time.Millisecond)
		}
		if err != nil {
			if errors.Is(err, errDestinationBusy) {
				return err
			}
			if job.Callback == nil {
				return err
			}
			completionErr := service.invokeCompletion(ctx, iamToken, completion{
				SchemaVersion: 1,
				JobID:         job.JobID,
				Success:       false,
				PublicURLs:    publicURLs,
				Callback:      job.Callback,
				Error:         err.Error(),
			})
			if completionErr != nil {
				return completionErr
			}
			for _, stagedFile := range job.Files {
				if cleanupErr := service.deleteStaged(ctx, job.Bucket, stagedFile, iamToken); cleanupErr != nil {
					fmt.Printf("delete staged object after failed job %s: %v\n", job.JobID, cleanupErr)
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
	return service.invokeCompletion(ctx, iamToken, completion{
		SchemaVersion: 1,
		JobID:         job.JobID,
		Success:       true,
		PublicURLs:    publicURLs,
		Callback:      job.Callback,
	})
}

// Handler is the Yandex Cloud Functions entry point. Async raw invocation
// passes the original JSON request body directly as this byte slice.
func Handler(ctx context.Context, body []byte) ([]byte, error) {
	var job uploadJob
	if err := json.Unmarshal(body, &job); err != nil {
		return nil, fmt.Errorf("decode upload job: %w", err)
	}
	if err := validateJob(&job); err != nil {
		return nil, err
	}
	service, err := serviceFromEnvironment()
	if err != nil {
		return nil, err
	}
	if err := service.process(ctx, job); err != nil {
		return nil, err
	}
	return json.Marshal(map[string]any{"job_id": job.JobID, "uploaded": len(job.Files)})
}
