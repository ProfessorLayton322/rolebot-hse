//go:build aws

package main

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	"github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

type fakeSSM struct {
	values map[string]string
	names  []string
}

type fakeAWSSQS struct {
	inputs []*sqs.SendMessageInput
}

func (client *fakeAWSSQS) SendMessage(
	_ context.Context,
	input *sqs.SendMessageInput,
	_ ...func(*sqs.Options),
) (*sqs.SendMessageOutput, error) {
	client.inputs = append(client.inputs, input)
	return &sqs.SendMessageOutput{}, nil
}

func TestAWSCommandPublisherUsesOneOrderedGroup(t *testing.T) {
	client := &fakeAWSSQS{}
	publisher := &awsCommandPublisher{client: client, fifoURL: "queue-url"}
	command := orderedCommand{
		SchemaVersion: 1,
		OperationID:   "11111111-1111-4111-8111-111111111111",
		EventID:       "event-a1",
		Operation:     "SCHEDULED_CLOSE_EVENT",
		Platform:      "system",
		Payload:       map[string]any{},
		ReplyContext:  emptyReplyContext(),
		CreatedAt:     time.Date(2026, 10, 11, 0, 0, 0, 0, time.UTC),
	}

	if err := publisher.publish(context.Background(), command); err != nil {
		t.Fatal(err)
	}
	if len(client.inputs) != 1 || aws.ToString(client.inputs[0].MessageGroupId) != awsOrderedMessageGroup {
		t.Fatalf("unexpected SQS inputs: %#v", client.inputs)
	}
}

func (client *fakeSSM) GetParameter(
	_ context.Context,
	input *ssm.GetParameterInput,
	_ ...func(*ssm.Options),
) (*ssm.GetParameterOutput, error) {
	name := aws.ToString(input.Name)
	client.names = append(client.names, name)
	value := client.values[name]
	return &ssm.GetParameterOutput{Parameter: &types.Parameter{Value: aws.String(value)}}, nil
}

func TestAWSRuntimeGateRequiresBothControls(t *testing.T) {
	t.Setenv("APPLICATION_MODE", "standby")
	t.Setenv("MUTATIONS_ENABLED", "false")
	if awsRuntimeEnabled() {
		t.Fatal("standby runtime accepted work")
	}
	t.Setenv("APPLICATION_MODE", "active")
	if awsRuntimeEnabled() {
		t.Fatal("active mode accepted work without mutation control")
	}
	t.Setenv("MUTATIONS_ENABLED", "true")
	if !awsRuntimeEnabled() {
		t.Fatal("active runtime rejected work")
	}
}

func TestAWSRuntimeConfigurationReadsOnlyNotifierSecrets(t *testing.T) {
	for _, key := range []string{"TG_ADMIN_IDS", "VK_ADMIN_IDS", "TG_GAMEMASTER_IDS", "VK_GAMEMASTER_IDS"} {
		t.Setenv(key, "[]")
	}
	client := &fakeSSM{values: map[string]string{
		"/larp-bot/production/cloudflare-egress-hmac-secret": "egress-secret",
		"/larp-bot/production/vk-access-token":               "vk-token",
	}}
	config, err := loadAWSRuntimeConfiguration(context.Background(), client, awsNotifierEnvironment{
		ParameterPrefix: "/larp-bot/production/",
	})
	if err != nil {
		t.Fatal(err)
	}
	if config.Values["AWS_TO_CF_EGRESS_HMAC_SECRET"] != "egress-secret" ||
		config.Values["VK_ACCESS_TOKEN"] != "vk-token" {
		t.Fatalf("unexpected runtime config: %#v", config.Values)
	}
	if len(client.names) != 2 {
		t.Fatalf("unexpected SSM reads: %#v", client.names)
	}
}

func TestAWSRuntimeConfigurationIsCachedForWarmInvocations(t *testing.T) {
	for _, key := range []string{"TG_ADMIN_IDS", "VK_ADMIN_IDS", "TG_GAMEMASTER_IDS", "VK_GAMEMASTER_IDS"} {
		t.Setenv(key, "[]")
	}
	awsRuntimeConfigCacheMu.Lock()
	awsRuntimeConfigCache = nil
	awsRuntimeConfigCacheMu.Unlock()
	t.Cleanup(func() {
		awsRuntimeConfigCacheMu.Lock()
		awsRuntimeConfigCache = nil
		awsRuntimeConfigCacheMu.Unlock()
	})
	first := &fakeSSM{values: map[string]string{
		"/larp-bot/production/cloudflare-egress-hmac-secret": "egress-secret",
		"/larp-bot/production/vk-access-token":               "vk-token",
	}}
	second := &fakeSSM{values: map[string]string{}}
	env := awsNotifierEnvironment{ParameterPrefix: "/larp-bot/production/"}

	if _, err := cachedAWSRuntimeConfiguration(context.Background(), first, env); err != nil {
		t.Fatal(err)
	}
	if _, err := cachedAWSRuntimeConfiguration(context.Background(), second, env); err != nil {
		t.Fatal(err)
	}
	if len(first.names) != 2 || len(second.names) != 0 {
		t.Fatalf("unexpected SSM reads: first=%#v second=%#v", first.names, second.names)
	}
}

func TestDynamoEventPayloadMapsLosslesslyToLifecycleEvent(t *testing.T) {
	payload, err := json.Marshal(map[string]any{
		"event_id":                              "event-a1",
		"name":                                  "Game",
		"game_date":                             "2026-10-20",
		"status":                                "CREATED",
		"registration_open_at":                  "2026-10-10T03:00:00Z",
		"registration_open_notified_recipients": []string{"telegram:1"},
		"registration_open_notification_completed_at": nil,
		"confirmation_open_at":                        "2026-10-11T03:00:00+00:00",
		"confirmation_deadline":                       "2026-10-12T03:00:00.123456+00:00",
		"scheduled_close_at":                          "2026-10-19T03:00:00Z",
		"scheduled_close_operation_id":                "operation-1",
		"scheduled_close_completed_at":                nil,
		"archived_at":                                 nil,
	})
	if err != nil {
		t.Fatal(err)
	}
	event, err := plannedEventFromPayload(string(payload))
	if err != nil {
		t.Fatal(err)
	}
	if event.EventID != "event-a1" || event.Name != "Game" || event.GameDate.Format("2006-01-02") != "2026-10-20" {
		t.Fatalf("unexpected event: %#v", event)
	}
	if event.ConfirmationDeadline == nil || event.ConfirmationDeadline.Nanosecond() != 123456000 {
		t.Fatalf("timestamp precision was not preserved: %#v", event.ConfirmationDeadline)
	}
	if !event.RegistrationOpenAt.Equal(time.Date(2026, 10, 10, 3, 0, 0, 0, time.UTC)) {
		t.Fatalf("unexpected registration time: %#v", event.RegistrationOpenAt)
	}
}
