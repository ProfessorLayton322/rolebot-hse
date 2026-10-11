package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestYandexUploaderRuntimeGateDefaultsToEnabledAndHonorsMaintenance(t *testing.T) {
	if !yandexUploaderEnabled() {
		t.Fatal("legacy runtime without controls must remain enabled")
	}
	t.Setenv("APPLICATION_MODE", "maintenance")
	t.Setenv("MUTATIONS_ENABLED", "false")
	if yandexUploaderEnabled() {
		t.Fatal("maintenance runtime accepted work")
	}
	t.Setenv("APPLICATION_MODE", "active")
	t.Setenv("MUTATIONS_ENABLED", "true")
	if !yandexUploaderEnabled() {
		t.Fatal("active runtime rejected work")
	}
}

func TestValidateJobRejectsObjectsOutsideItsStagingDirectory(t *testing.T) {
	job := uploadJob{
		SchemaVersion: 1,
		JobID:         "12345678-1234-1234-1234-123456789abc",
		Bucket:        "private-bucket",
		Files: []uploadFile{{
			Role:        "master",
			ObjectKey:   "larp-bot/uploads/a-different-job/master.xlsx",
			DiskPath:    "disk:/larp-bot/events/game.xlsx",
			ContentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
			SHA256:      strings.Repeat("a", 64),
			Generation:  "00000000000000000001-job",
			Publish:     true,
		}},
	}

	if err := validateJob(&job); err == nil {
		t.Fatal("validateJob accepted an object from another staging directory")
	}
}

func TestProcessStreamsPublishesDeletesAndCallsCompletion(t *testing.T) {
	content := []byte("workbook bytes")
	digest := sha256.Sum256(content)
	jobID := "12345678-1234-1234-1234-123456789abc"
	objectKey := stagingPrefix + jobID + "/master.xlsx"
	diskPath := "disk:/larp-bot/events/game.xlsx"
	generation := "00000000000000000001-job"
	published := false
	deleted := false
	lockReleased := false
	previousGenerationDeleted := false
	var completionPayload completion

	var server *httptest.Server
	server = httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch {
		case request.URL.Path == "/metadata":
			if request.Header.Get("Metadata-Flavor") != "Google" {
				t.Error("metadata request did not opt in to service-account metadata")
			}
			io.WriteString(writer, `{"access_token":"iam-token"}`)
		case request.URL.Path == "/token":
			io.WriteString(writer, `{"access_token":"identity-token"}`)
		case request.URL.Path == "/config":
			if request.Header.Get("Authorization") != "Bearer identity-token" {
				t.Errorf("unexpected runtime-config authorization: %q", request.Header.Get("Authorization"))
			}
			io.WriteString(writer, `{"values":{"YANDEX_DISK_TOKEN":"disk-token"}}`)
		case request.Method == http.MethodGet && request.URL.Path == "/private-bucket/"+stateKey(diskPath):
			writer.Header().Set("X-Amz-Version-Id", "old-generation-version")
			io.WriteString(writer, "00000000000000000000-old")
		case request.Method == http.MethodPut && request.URL.Path == "/private-bucket/"+lockKey(diskPath):
			if request.Header.Get("If-None-Match") != "*" {
				t.Error("destination lock was not created conditionally")
			}
			writer.Header().Set("X-Amz-Version-Id", "lock-version")
			writer.WriteHeader(http.StatusOK)
		case request.Method == http.MethodDelete && request.URL.Path == "/private-bucket/"+lockKey(diskPath):
			if request.URL.Query().Get("versionId") != "lock-version" {
				t.Errorf("unexpected lock version: %q", request.URL.Query().Get("versionId"))
			}
			lockReleased = true
			writer.WriteHeader(http.StatusNoContent)
		case request.Method == http.MethodGet && request.URL.Path == "/private-bucket/"+objectKey:
			writer.Header().Set("X-Amz-Meta-Sha256", hex.EncodeToString(digest[:]))
			writer.Header().Set("X-Amz-Meta-Generation", generation)
			writer.Write(content)
		case request.Method == http.MethodPut && request.URL.Path == "/disk/resources":
			writer.WriteHeader(http.StatusCreated)
		case request.Method == http.MethodGet && request.URL.Path == "/disk/resources/upload":
			io.WriteString(writer, `{"href":"`+server.URL+`/transfer"}`)
		case request.Method == http.MethodPut && request.URL.Path == "/transfer":
			body, err := io.ReadAll(request.Body)
			if err != nil {
				t.Fatal(err)
			}
			if string(body) != string(content) {
				t.Errorf("unexpected streamed body: %q", body)
			}
			writer.WriteHeader(http.StatusCreated)
		case request.Method == http.MethodPut && request.URL.Path == "/disk/resources/publish":
			published = true
			writer.WriteHeader(http.StatusOK)
		case request.Method == http.MethodGet && request.URL.Path == "/disk/resources":
			io.WriteString(writer, `{"public_url":"https://disk.example/game"}`)
		case request.Method == http.MethodPut && request.URL.Path == "/private-bucket/"+stateKey(diskPath):
			body, _ := io.ReadAll(request.Body)
			if string(body) != generation {
				t.Errorf("unexpected generation marker: %q", body)
			}
			writer.Header().Set("X-Amz-Version-Id", "generation-version")
			writer.WriteHeader(http.StatusOK)
		case request.Method == http.MethodDelete && request.URL.Path == "/private-bucket/"+stateKey(diskPath):
			if request.URL.Query().Get("versionId") != "old-generation-version" {
				t.Errorf("unexpected generation version: %q", request.URL.Query().Get("versionId"))
			}
			previousGenerationDeleted = true
			writer.WriteHeader(http.StatusNoContent)
		case request.Method == http.MethodDelete && request.URL.Path == "/private-bucket/"+objectKey:
			if !published {
				t.Error("staged object was deleted before publication completed")
			}
			if request.URL.Query().Get("versionId") != "staged-version" {
				t.Errorf("unexpected staged version: %q", request.URL.Query().Get("versionId"))
			}
			deleted = true
			writer.WriteHeader(http.StatusNoContent)
		case request.Method == http.MethodPost && request.URL.Path == "/completion":
			if request.URL.Query().Get("integration") != "async" {
				t.Error("completion was not invoked asynchronously")
			}
			if !deleted {
				t.Error("completion was invoked before staged-object cleanup")
			}
			if !lockReleased {
				t.Error("completion was invoked before the destination lock was released")
			}
			if err := json.NewDecoder(request.Body).Decode(&completionPayload); err != nil {
				t.Fatal(err)
			}
			writer.WriteHeader(http.StatusAccepted)
		default:
			t.Errorf("unexpected request: %s %s", request.Method, request.URL.String())
			http.Error(writer, "unexpected request", http.StatusNotFound)
		}
	}))
	defer server.Close()

	service := uploaderService{
		env: environment{
			Bucket:                  "private-bucket",
			RuntimeConfigURL:        server.URL + "/config",
			RuntimeConfigAudience:   "https://config.example",
			RuntimeServiceAccountID: "service-account-id",
			CompletionFunctionURL:   server.URL + "/completion",
		},
		client:     server.Client(),
		metadata:   server.URL + "/metadata",
		tokenURL:   server.URL + "/token",
		diskAPI:    server.URL + "/disk",
		storageURL: server.URL,
	}
	job := uploadJob{
		SchemaVersion: 1,
		JobID:         jobID,
		Bucket:        "private-bucket",
		Files: []uploadFile{{
			Role:          "master",
			ObjectKey:     objectKey,
			ObjectVersion: "staged-version",
			DiskPath:      diskPath,
			ContentType:   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
			SHA256:        hex.EncodeToString(digest[:]),
			Generation:    generation,
			Publish:       true,
		}},
		Callback: map[string]any{"kind": "event_created"},
	}

	if err := service.process(context.Background(), job); err != nil {
		t.Fatal(err)
	}
	if !deleted {
		t.Fatal("staged object was not deleted")
	}
	if !previousGenerationDeleted {
		t.Fatal("previous generation version was not deleted")
	}
	if !completionPayload.Success || completionPayload.PublicURLs["master"] != "https://disk.example/game" {
		t.Fatalf("unexpected completion payload: %#v", completionPayload)
	}
}

func TestObjectURLKeepsObjectKeySegments(t *testing.T) {
	service := uploaderService{storageURL: "https://storage.example"}
	got := service.objectURL("bucket", "larp-bot/uploads/job/file name.xlsx")
	want := "https://storage.example/bucket/larp-bot/uploads/job/file%20name.xlsx"
	if got != want {
		t.Fatalf("objectURL() = %q, want %q", got, want)
	}
	if _, err := url.Parse(got); err != nil {
		t.Fatal(err)
	}
}

func TestFailedCallbackJobDeletesItsExactStagedVersion(t *testing.T) {
	jobID := "12345678-1234-1234-1234-123456789abc"
	diskPath := "disk:/larp-bot/events/game.xlsx"
	objectKey := stagingPrefix + jobID + "/master.xlsx"
	lockCount := 0
	completionSeen := false
	stagedDeleted := false
	var completionPayload completion

	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch {
		case request.URL.Path == "/metadata":
			io.WriteString(writer, `{"access_token":"iam-token"}`)
		case request.URL.Path == "/token":
			io.WriteString(writer, `{"access_token":"identity-token"}`)
		case request.URL.Path == "/config":
			io.WriteString(writer, `{"values":{"YANDEX_DISK_TOKEN":"disk-token"}}`)
		case request.Method == http.MethodPut && request.URL.Path == "/private-bucket/"+lockKey(diskPath):
			lockCount++
			writer.Header().Set("X-Amz-Version-Id", fmt.Sprintf("lock-version-%d", lockCount))
			writer.WriteHeader(http.StatusOK)
		case request.Method == http.MethodDelete && request.URL.Path == "/private-bucket/"+lockKey(diskPath):
			writer.WriteHeader(http.StatusNoContent)
		case request.Method == http.MethodGet && request.URL.Path == "/private-bucket/"+stateKey(diskPath):
			http.Error(writer, "state unavailable", http.StatusServiceUnavailable)
		case request.Method == http.MethodPost && request.URL.Path == "/completion":
			completionSeen = true
			if err := json.NewDecoder(request.Body).Decode(&completionPayload); err != nil {
				t.Fatal(err)
			}
			writer.WriteHeader(http.StatusAccepted)
		case request.Method == http.MethodDelete && request.URL.Path == "/private-bucket/"+objectKey:
			if !completionSeen {
				t.Error("staged object was deleted before the failure callback was accepted")
			}
			if request.URL.Query().Get("versionId") != "staged-version" {
				t.Errorf("unexpected staged version: %q", request.URL.Query().Get("versionId"))
			}
			stagedDeleted = true
			writer.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected request: %s %s", request.Method, request.URL.String())
			http.Error(writer, "unexpected request", http.StatusNotFound)
		}
	}))
	defer server.Close()

	service := uploaderService{
		env: environment{
			Bucket:                  "private-bucket",
			RuntimeConfigURL:        server.URL + "/config",
			RuntimeConfigAudience:   "https://config.example",
			RuntimeServiceAccountID: "service-account-id",
			CompletionFunctionURL:   server.URL + "/completion",
		},
		client:     server.Client(),
		metadata:   server.URL + "/metadata",
		tokenURL:   server.URL + "/token",
		diskAPI:    server.URL + "/disk",
		storageURL: server.URL,
	}
	job := uploadJob{
		SchemaVersion: 1,
		JobID:         jobID,
		Bucket:        "private-bucket",
		Files: []uploadFile{{
			Role:          "master",
			ObjectKey:     objectKey,
			ObjectVersion: "staged-version",
			DiskPath:      diskPath,
			ContentType:   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
			SHA256:        strings.Repeat("a", 64),
			Generation:    "00000000000000000001-job",
			Publish:       true,
		}},
		Callback: map[string]any{"kind": "event_created"},
	}

	if err := service.process(context.Background(), job); err != nil {
		t.Fatal(err)
	}
	if lockCount != 3 {
		t.Fatalf("upload attempts = %d, want 3", lockCount)
	}
	if !completionSeen || completionPayload.Success {
		t.Fatalf("unexpected completion payload: %#v", completionPayload)
	}
	if !stagedDeleted {
		t.Fatal("failed job left its staged object behind")
	}
}

func TestGoogleMirroredJobCompletesWhenYandexTokenIsUnavailable(t *testing.T) {
	jobID := "12345678-1234-1234-1234-123456789abc"
	objectKey := stagingPrefix + jobID + "/public.xlsx"
	completionSeen := false
	stagedDeleted := false
	var completionPayload completion

	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch {
		case request.URL.Path == "/metadata":
			io.WriteString(writer, `{"access_token":"iam-token"}`)
		case request.URL.Path == "/token":
			io.WriteString(writer, `{"access_token":"identity-token"}`)
		case request.URL.Path == "/config":
			io.WriteString(writer, `{"values":{}}`)
		case request.Method == http.MethodPost && request.URL.Path == "/completion":
			completionSeen = true
			if err := json.NewDecoder(request.Body).Decode(&completionPayload); err != nil {
				t.Fatal(err)
			}
			writer.WriteHeader(http.StatusAccepted)
		case request.Method == http.MethodDelete && request.URL.Path == "/private-bucket/"+objectKey:
			stagedDeleted = true
			writer.WriteHeader(http.StatusNoContent)
		default:
			t.Errorf("unexpected request: %s %s", request.Method, request.URL.String())
			http.Error(writer, "unexpected request", http.StatusNotFound)
		}
	}))
	defer server.Close()

	service := uploaderService{
		env: environment{
			Bucket:                  "private-bucket",
			RuntimeConfigURL:        server.URL + "/config",
			RuntimeConfigAudience:   "https://config.example",
			RuntimeServiceAccountID: "service-account-id",
			CompletionFunctionURL:   server.URL + "/completion",
		},
		client:     server.Client(),
		metadata:   server.URL + "/metadata",
		tokenURL:   server.URL + "/token",
		storageURL: server.URL,
	}
	job := uploadJob{
		SchemaVersion: 1,
		JobID:         jobID,
		Bucket:        "private-bucket",
		Files: []uploadFile{{
			Role:           "public",
			ObjectKey:      objectKey,
			ObjectVersion:  "staged-version",
			DiskPath:       "disk:/larp-bot/events/game/public.xlsx",
			ContentType:    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
			SHA256:         strings.Repeat("a", 64),
			Generation:     "00000000000000000001-job",
			Publish:        true,
			GoogleMirrored: true,
			GoogleURL:      "https://drive.example/public",
		}},
		Callback: map[string]any{
			"kind": "event_tables",
			"google_public_urls": map[string]any{
				"public": "https://drive.example/public",
			},
		},
	}

	if err := service.process(context.Background(), job); err != nil {
		t.Fatal(err)
	}
	if !completionSeen || completionPayload.Success {
		t.Fatalf("unexpected completion payload: %#v", completionPayload)
	}
	if completionPayload.Error != "runtime configuration has no Yandex Disk token" {
		t.Fatalf("unexpected optional Yandex error: %q", completionPayload.Error)
	}
	if !stagedDeleted {
		t.Fatal("Google-mirrored job left its staged object behind")
	}
}

func TestStaleDestinationLockIsTakenOverConditionally(t *testing.T) {
	jobID := "12345678-1234-1234-1234-123456789abc"
	diskPath := "disk:/larp-bot/events/game.xlsx"
	oldGeneration := fmt.Sprintf("%020d-%s", time.Now().Add(-7*time.Minute).UnixNano(), jobID)
	newGeneration := fmt.Sprintf("%020d-%s", time.Now().UnixNano(), jobID)
	putSeen := false
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		switch request.Method {
		case http.MethodGet:
			writer.Header().Set("ETag", `"old-lock-etag"`)
			writer.Header().Set("X-Amz-Version-Id", "stale-lock-version")
			io.WriteString(writer, oldGeneration)
		case http.MethodPut:
			putSeen = true
			if request.Header.Get("If-Match") != `"old-lock-etag"` {
				t.Errorf("unexpected takeover precondition: %q", request.Header.Get("If-Match"))
			}
			body, _ := io.ReadAll(request.Body)
			if string(body) != newGeneration {
				t.Errorf("unexpected takeover generation: %q", body)
			}
			writer.Header().Set("X-Amz-Version-Id", "new-lock-version")
			writer.WriteHeader(http.StatusOK)
		case http.MethodDelete:
			if request.URL.Query().Get("versionId") != "stale-lock-version" {
				t.Errorf("unexpected deleted stale version: %q", request.URL.Query().Get("versionId"))
			}
			writer.WriteHeader(http.StatusNoContent)
		default:
			t.Fatalf("unexpected method: %s", request.Method)
		}
	}))
	defer server.Close()
	service := uploaderService{client: server.Client(), storageURL: server.URL}

	versionID, takenOver, err := service.takeOverStaleDestinationLock(
		context.Background(),
		"private-bucket",
		uploadFile{DiskPath: diskPath, Generation: newGeneration},
		"iam-token",
	)

	if err != nil {
		t.Fatal(err)
	}
	if !takenOver || !putSeen {
		t.Fatal("stale lock was not taken over")
	}
	if versionID != "new-lock-version" {
		t.Fatalf("unexpected new lock version: %q", versionID)
	}
}
