//go:build aws

package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/feature/dynamodb/attributevalue"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb"
	"github.com/aws/aws-sdk-go-v2/service/dynamodb/types"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	"github.com/aws/smithy-go"
)

var registrationNotificationBacklogCutoff = time.Date(2026, time.October, 2, 16, 15, 0, 0, time.UTC)

type awsNotifierEnvironment struct {
	EventsTable       string
	FIFOURL           string
	ParameterPrefix   string
	TelegramEgressURL string
}

func loadAWSNotifierEnvironment() (awsNotifierEnvironment, error) {
	env := awsNotifierEnvironment{
		EventsTable:       os.Getenv("DYNAMODB_EVENTS_TABLE"),
		FIFOURL:           os.Getenv("AWS_FIFO_QUEUE_URL"),
		ParameterPrefix:   os.Getenv("AWS_PARAMETER_PREFIX"),
		TelegramEgressURL: os.Getenv("TELEGRAM_EGRESS_URL"),
	}
	if env.EventsTable == "" || env.FIFOURL == "" || env.ParameterPrefix == "" || env.TelegramEgressURL == "" {
		return awsNotifierEnvironment{}, errors.New("AWS registration notifier environment is incomplete")
	}
	if !strings.HasSuffix(env.ParameterPrefix, "/") {
		env.ParameterPrefix += "/"
	}
	return env, nil
}

func awsRuntimeEnabled() bool {
	return os.Getenv("APPLICATION_MODE") == "active" && os.Getenv("MUTATIONS_ENABLED") == "true"
}

type ssmGetter interface {
	GetParameter(context.Context, *ssm.GetParameterInput, ...func(*ssm.Options)) (*ssm.GetParameterOutput, error)
}

func loadAWSRuntimeConfiguration(
	ctx context.Context,
	client ssmGetter,
	env awsNotifierEnvironment,
) (runtimeConfiguration, error) {
	values := map[string]string{
		"TG_ADMIN_IDS":      os.Getenv("TG_ADMIN_IDS"),
		"VK_ADMIN_IDS":      os.Getenv("VK_ADMIN_IDS"),
		"TG_GAMEMASTER_IDS": os.Getenv("TG_GAMEMASTER_IDS"),
		"VK_GAMEMASTER_IDS": os.Getenv("VK_GAMEMASTER_IDS"),
	}
	for key, suffix := range map[string]string{
		"AWS_TO_CF_EGRESS_HMAC_SECRET": "cloudflare-egress-hmac-secret",
		"VK_ACCESS_TOKEN":              "vk-access-token",
	} {
		response, err := client.GetParameter(ctx, &ssm.GetParameterInput{
			Name:           aws.String(env.ParameterPrefix + suffix),
			WithDecryption: aws.Bool(true),
		})
		if err != nil {
			return runtimeConfiguration{}, fmt.Errorf("read runtime parameter %s: %w", suffix, err)
		}
		if response.Parameter == nil || response.Parameter.Value == nil || *response.Parameter.Value == "" {
			return runtimeConfiguration{}, fmt.Errorf("runtime parameter is empty: %s", suffix)
		}
		values[key] = *response.Parameter.Value
	}
	return runtimeConfiguration{Values: values}, nil
}

var (
	awsRuntimeConfigCache   *runtimeConfiguration
	awsRuntimeConfigCacheMu sync.Mutex
)

func cachedAWSRuntimeConfiguration(
	ctx context.Context,
	client ssmGetter,
	env awsNotifierEnvironment,
) (runtimeConfiguration, error) {
	awsRuntimeConfigCacheMu.Lock()
	defer awsRuntimeConfigCacheMu.Unlock()
	if awsRuntimeConfigCache != nil {
		return *awsRuntimeConfigCache, nil
	}
	config, err := loadAWSRuntimeConfiguration(ctx, client, env)
	if err != nil {
		return runtimeConfiguration{}, err
	}
	awsRuntimeConfigCache = &config
	return config, nil
}

type dynamoEventClient interface {
	GetItem(context.Context, *dynamodb.GetItemInput, ...func(*dynamodb.Options)) (*dynamodb.GetItemOutput, error)
	Scan(context.Context, *dynamodb.ScanInput, ...func(*dynamodb.Options)) (*dynamodb.ScanOutput, error)
	UpdateItem(context.Context, *dynamodb.UpdateItemInput, ...func(*dynamodb.Options)) (*dynamodb.UpdateItemOutput, error)
}

type dynamoEventRow struct {
	EventID    string `dynamodbav:"event_id"`
	RecordType string `dynamodbav:"record_type"`
	Payload    string `dynamodbav:"payload"`
	Revision   int64  `dynamodbav:"_revision"`
}

type dynamoEventDocument struct {
	EventID                 string   `json:"event_id"`
	Name                    string   `json:"name"`
	GameDate                *string  `json:"game_date"`
	Status                  string   `json:"status"`
	RegistrationOpenAt      *string  `json:"registration_open_at"`
	RegistrationRecipients  []string `json:"registration_open_notified_recipients"`
	RegistrationCompleted   *string  `json:"registration_open_notification_completed_at"`
	ConfirmationOpenAt      *string  `json:"confirmation_open_at"`
	ConfirmationDeadline    *string  `json:"confirmation_deadline"`
	ScheduledCloseAt        *string  `json:"scheduled_close_at"`
	ScheduledCloseOperation *string  `json:"scheduled_close_operation_id"`
	ScheduledCloseCompleted *string  `json:"scheduled_close_completed_at"`
	ArchivedAt              *string  `json:"archived_at"`
}

func parseOptionalTime(value *string) (*time.Time, error) {
	if value == nil {
		return nil, nil
	}
	parsed, err := time.Parse(time.RFC3339Nano, *value)
	if err != nil {
		return nil, err
	}
	parsed = parsed.UTC()
	return &parsed, nil
}

func parseOptionalDate(value *string) (*time.Time, error) {
	if value == nil {
		return nil, nil
	}
	parsed, err := time.Parse("2006-01-02", *value)
	if err != nil {
		return nil, err
	}
	return &parsed, nil
}

func plannedEventFromPayload(payload string) (plannedEvent, error) {
	var document dynamoEventDocument
	if err := json.Unmarshal([]byte(payload), &document); err != nil {
		return plannedEvent{}, err
	}
	gameDate, err := parseOptionalDate(document.GameDate)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse game date for %s: %w", document.EventID, err)
	}
	registrationOpen, err := parseOptionalTime(document.RegistrationOpenAt)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse registration opening for %s: %w", document.EventID, err)
	}
	registrationCompleted, err := parseOptionalTime(document.RegistrationCompleted)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse registration completion for %s: %w", document.EventID, err)
	}
	confirmationOpen, err := parseOptionalTime(document.ConfirmationOpenAt)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse confirmation opening for %s: %w", document.EventID, err)
	}
	confirmationDeadline, err := parseOptionalTime(document.ConfirmationDeadline)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse confirmation deadline for %s: %w", document.EventID, err)
	}
	scheduledClose, err := parseOptionalTime(document.ScheduledCloseAt)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse scheduled close for %s: %w", document.EventID, err)
	}
	scheduledCloseCompleted, err := parseOptionalTime(document.ScheduledCloseCompleted)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse scheduled close completion for %s: %w", document.EventID, err)
	}
	archivedAt, err := parseOptionalTime(document.ArchivedAt)
	if err != nil {
		return plannedEvent{}, fmt.Errorf("parse archive timestamp for %s: %w", document.EventID, err)
	}
	return plannedEvent{
		EventID:                 document.EventID,
		Name:                    document.Name,
		GameDate:                gameDate,
		Status:                  document.Status,
		RegistrationOpenAt:      registrationOpen,
		RegistrationRecipients:  append([]string(nil), document.RegistrationRecipients...),
		RegistrationCompleted:   registrationCompleted,
		ConfirmationOpenAt:      confirmationOpen,
		ConfirmationDeadline:    confirmationDeadline,
		ScheduledCloseAt:        scheduledClose,
		ScheduledCloseOperation: document.ScheduledCloseOperation,
		ScheduledCloseCompleted: scheduledCloseCompleted,
		ArchivedAt:              archivedAt,
	}, nil
}

type awsEventRepository struct {
	client dynamoEventClient
	table  string
}

func (repository *awsEventRepository) scanRows(ctx context.Context) ([]dynamoEventRow, error) {
	rows := make([]dynamoEventRow, 0)
	var startKey map[string]types.AttributeValue
	for {
		response, err := repository.client.Scan(ctx, &dynamodb.ScanInput{
			TableName:         aws.String(repository.table),
			ConsistentRead:    aws.Bool(true),
			ExclusiveStartKey: startKey,
		})
		if err != nil {
			return nil, err
		}
		for _, item := range response.Items {
			var row dynamoEventRow
			if err := attributevalue.UnmarshalMap(item, &row); err != nil {
				return nil, err
			}
			if row.RecordType == "event" {
				rows = append(rows, row)
			}
		}
		if len(response.LastEvaluatedKey) == 0 {
			return rows, nil
		}
		startKey = response.LastEvaluatedKey
	}
}

func isConditionalFailure(err error) bool {
	var apiError smithy.APIError
	return errors.As(err, &apiError) && apiError.ErrorCode() == "ConditionalCheckFailedException"
}

func (repository *awsEventRepository) getRow(ctx context.Context, eventID string) (dynamoEventRow, error) {
	response, err := repository.client.GetItem(ctx, &dynamodb.GetItemInput{
		TableName:      aws.String(repository.table),
		Key:            map[string]types.AttributeValue{"event_id": &types.AttributeValueMemberS{Value: eventID}},
		ConsistentRead: aws.Bool(true),
	})
	if err != nil {
		return dynamoEventRow{}, err
	}
	if len(response.Item) == 0 {
		return dynamoEventRow{}, errors.New("event not found")
	}
	var row dynamoEventRow
	if err := attributevalue.UnmarshalMap(response.Item, &row); err != nil {
		return dynamoEventRow{}, err
	}
	if row.RecordType != "event" {
		return dynamoEventRow{}, errors.New("DynamoDB item is not an event")
	}
	return row, nil
}

func (repository *awsEventRepository) mutate(
	ctx context.Context,
	eventID string,
	mutation func(map[string]any) (bool, error),
) (bool, error) {
	for attempt := 0; attempt < 4; attempt++ {
		row, err := repository.getRow(ctx, eventID)
		if err != nil {
			return false, err
		}
		var payload map[string]any
		decoder := json.NewDecoder(strings.NewReader(row.Payload))
		decoder.UseNumber()
		if err := decoder.Decode(&payload); err != nil {
			return false, err
		}
		changed, err := mutation(payload)
		if err != nil || !changed {
			return changed, err
		}
		encoded, err := json.Marshal(payload)
		if err != nil {
			return false, err
		}
		_, err = repository.client.UpdateItem(ctx, &dynamodb.UpdateItemInput{
			TableName:           aws.String(repository.table),
			Key:                 map[string]types.AttributeValue{"event_id": &types.AttributeValueMemberS{Value: eventID}},
			UpdateExpression:    aws.String("SET payload = :payload, #revision = :next_revision"),
			ConditionExpression: aws.String("#revision = :expected_revision AND record_type = :event_type"),
			ExpressionAttributeNames: map[string]string{
				"#revision": "_revision",
			},
			ExpressionAttributeValues: map[string]types.AttributeValue{
				":payload":           &types.AttributeValueMemberS{Value: string(encoded)},
				":next_revision":     &types.AttributeValueMemberN{Value: strconv.FormatInt(row.Revision+1, 10)},
				":expected_revision": &types.AttributeValueMemberN{Value: strconv.FormatInt(row.Revision, 10)},
				":event_type":        &types.AttributeValueMemberS{Value: "event"},
			},
		})
		if err == nil {
			return true, nil
		}
		if !isConditionalFailure(err) || attempt == 3 {
			return false, err
		}
	}
	return false, errors.New("unreachable DynamoDB retry loop")
}

func timeValue(payload map[string]any, key string) (*time.Time, error) {
	raw, ok := payload[key]
	if !ok || raw == nil {
		return nil, nil
	}
	value, ok := raw.(string)
	if !ok {
		return nil, fmt.Errorf("%s is not a string", key)
	}
	return parseOptionalTime(&value)
}

func (repository *awsEventRepository) completeRegistrationNotificationsBefore(
	ctx context.Context,
	openedBefore time.Time,
	completedAt time.Time,
) (int, error) {
	rows, err := repository.scanRows(ctx)
	if err != nil {
		return 0, err
	}
	completed := 0
	for _, row := range rows {
		event, err := plannedEventFromPayload(row.Payload)
		if err != nil {
			return completed, err
		}
		if event.RegistrationOpenAt == nil || event.RegistrationOpenAt.After(openedBefore) || event.RegistrationCompleted != nil {
			continue
		}
		changed, err := repository.mutate(ctx, row.EventID, func(payload map[string]any) (bool, error) {
			openAt, err := timeValue(payload, "registration_open_at")
			if err != nil {
				return false, err
			}
			if openAt == nil || openAt.After(openedBefore) || payload["registration_open_notification_completed_at"] != nil {
				return false, nil
			}
			payload["registration_open_notification_completed_at"] = completedAt.UTC().Format(time.RFC3339Nano)
			payload["updated_at"] = completedAt.UTC().Format(time.RFC3339Nano)
			return true, nil
		})
		if err != nil {
			return completed, err
		}
		if changed {
			completed++
		}
	}
	return completed, nil
}

func (repository *awsEventRepository) listBefore(
	ctx context.Context,
	dueBefore time.Time,
	limit int,
) ([]plannedEvent, error) {
	rows, err := repository.scanRows(ctx)
	if err != nil {
		return nil, err
	}
	events := make([]plannedEvent, 0)
	for _, row := range rows {
		event, err := plannedEventFromPayload(row.Payload)
		if err != nil {
			return nil, err
		}
		if registrationDue(event, dueBefore) || confirmationDue(event, dueBefore) || closingDue(event, dueBefore) {
			events = append(events, event)
		}
	}
	sort.Slice(events, func(left, right int) bool { return events[left].EventID < events[right].EventID })
	if len(events) > limit {
		events = events[:limit]
	}
	return events, nil
}

func (repository *awsEventRepository) openRegistrationNow(
	ctx context.Context,
	eventID string,
	scheduledAt time.Time,
	openedAt time.Time,
) (bool, error) {
	return repository.mutate(ctx, eventID, func(payload map[string]any) (bool, error) {
		current, err := timeValue(payload, "registration_open_at")
		if err != nil {
			return false, err
		}
		if current == nil || !current.Equal(scheduledAt) || !current.After(openedAt) ||
			payload["registration_open_notification_completed_at"] != nil || payload["archived_at"] != nil ||
			payload["status"] == "CLOSED" {
			return false, nil
		}
		payload["registration_open_at"] = openedAt.UTC().Format(time.RFC3339Nano)
		payload["updated_at"] = openedAt.UTC().Format(time.RFC3339Nano)
		return true, nil
	})
}

func (repository *awsEventRepository) markRegistrationDelivered(
	ctx context.Context,
	eventID string,
	recipients []string,
	updatedAt time.Time,
) error {
	_, err := repository.mutate(ctx, eventID, func(payload map[string]any) (bool, error) {
		if payload["registration_open_notification_completed_at"] != nil {
			return false, nil
		}
		merged := make([]string, 0, len(recipients))
		seen := make(map[string]struct{}, len(recipients))
		if current, ok := payload["registration_open_notified_recipients"].([]any); ok {
			for _, item := range current {
				value, ok := item.(string)
				if ok {
					if _, duplicate := seen[value]; !duplicate {
						seen[value] = struct{}{}
						merged = append(merged, value)
					}
				}
			}
		}
		for _, value := range recipients {
			if _, duplicate := seen[value]; duplicate {
				continue
			}
			seen[value] = struct{}{}
			merged = append(merged, value)
		}
		payload["registration_open_notified_recipients"] = merged
		payload["updated_at"] = updatedAt.UTC().Format(time.RFC3339Nano)
		return true, nil
	})
	return err
}

func (repository *awsEventRepository) completeRegistration(
	ctx context.Context,
	eventID string,
	completedAt time.Time,
) error {
	_, err := repository.mutate(ctx, eventID, func(payload map[string]any) (bool, error) {
		if payload["registration_open_notification_completed_at"] != nil {
			return false, nil
		}
		payload["registration_open_notification_completed_at"] = completedAt.UTC().Format(time.RFC3339Nano)
		payload["updated_at"] = completedAt.UTC().Format(time.RFC3339Nano)
		return true, nil
	})
	return err
}

type awsSQSSender interface {
	SendMessage(context.Context, *sqs.SendMessageInput, ...func(*sqs.Options)) (*sqs.SendMessageOutput, error)
}

type awsCommandPublisher struct {
	client  awsSQSSender
	fifoURL string
}

const awsOrderedMessageGroup = "ordered"

func (publisher *awsCommandPublisher) publish(ctx context.Context, command orderedCommand) error {
	body, err := marshalCommand(command)
	if err != nil {
		return err
	}
	_, err = publisher.client.SendMessage(ctx, &sqs.SendMessageInput{
		QueueUrl:               aws.String(publisher.fifoURL),
		MessageBody:            aws.String(body),
		MessageGroupId:         aws.String(awsOrderedMessageGroup),
		MessageDeduplicationId: aws.String(command.OperationID),
	})
	if err != nil {
		return fmt.Errorf("send AWS FIFO message: %w", err)
	}
	return nil
}

func handleAWSSchedule(ctx context.Context, _ json.RawMessage) (map[string]any, error) {
	if !awsRuntimeEnabled() {
		return map[string]any{"processed": false}, nil
	}
	env, err := loadAWSNotifierEnvironment()
	if err != nil {
		return nil, err
	}
	config, err := awsconfig.LoadDefaultConfig(ctx)
	if err != nil {
		return nil, err
	}
	runtimeConfig, err := cachedAWSRuntimeConfiguration(ctx, ssm.NewFromConfig(config), env)
	if err != nil {
		return nil, err
	}
	notifier, err := newDirectNotifier(
		&http.Client{Timeout: 10 * time.Second},
		environment{TelegramEgressURL: env.TelegramEgressURL},
		runtimeConfig,
	)
	if err != nil {
		return nil, err
	}
	service := scheduler{
		events:                    &awsEventRepository{client: dynamodb.NewFromConfig(config), table: env.EventsTable},
		recipients:                runtimeConfig,
		notifier:                  notifier,
		publisher:                 &awsCommandPublisher{client: sqs.NewFromConfig(config), fifoURL: env.FIFOURL},
		registrationBacklogCutoff: &registrationNotificationBacklogCutoff,
	}
	result, err := service.run(ctx, time.Now().UTC())
	if err != nil {
		return nil, err
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return nil, err
	}
	response := map[string]any{"processed": true}
	if err := json.Unmarshal(encoded, &response); err != nil {
		return nil, err
	}
	response["processed"] = true
	return response, nil
}

func main() {
	lambda.Start(handleAWSSchedule)
}
