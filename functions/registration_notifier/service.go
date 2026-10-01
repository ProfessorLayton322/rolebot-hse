package main

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"time"
)

const (
	invocationInterval = 5 * time.Minute
	perActionLimit     = 100
)

type plannedEvent struct {
	EventID                 string
	Name                    string
	GameDate                *time.Time
	Status                  string
	RegistrationOpenAt      *time.Time
	RegistrationRecipients  []string
	RegistrationCompleted   *time.Time
	ConfirmationOpenAt      *time.Time
	ConfirmationDeadline    *time.Time
	ScheduledCloseAt        *time.Time
	ScheduledCloseOperation *string
	ScheduledCloseCompleted *time.Time
	ArchivedAt              *time.Time
}

type identity struct {
	Platform string
	UserID   int64
}

func (i identity) key() string {
	return fmt.Sprintf("%s:%d", i.Platform, i.UserID)
}

type eventRepository interface {
	listBefore(context.Context, time.Time, int) ([]plannedEvent, error)
	openRegistrationNow(context.Context, string, time.Time, time.Time) (bool, error)
	markRegistrationDelivered(context.Context, string, []string, time.Time) error
	completeRegistration(context.Context, string, time.Time) error
}

type recipientProvider interface {
	registrationRecipients() ([]identity, error)
}

type notificationSender interface {
	sendRegistrationOpening(context.Context, plannedEvent, identity) error
}

type commandPublisher interface {
	publish(context.Context, orderedCommand) error
}

type scheduler struct {
	events     eventRepository
	recipients recipientProvider
	notifier   notificationSender
	publisher  commandPublisher
}

type runResult struct {
	EventsCompleted              int `json:"events_completed"`
	NotificationsDelivered       int `json:"notifications_delivered"`
	ConfirmationCommandsEnqueued int `json:"confirmation_commands_enqueued"`
	ClosingCommandsEnqueued      int `json:"closing_commands_enqueued"`
}

// nextInvocation returns the next wall-clock boundary selected by the */5
// timer. Events exactly on that boundary belong to that invocation; events
// before it are deliberately dispatched by the current invocation.
func nextInvocation(now time.Time) time.Time {
	return now.UTC().Truncate(invocationInterval).Add(invocationInterval)
}

func beforeNextInvocation(instant *time.Time, cutoff time.Time) bool {
	return instant != nil && instant.Before(cutoff)
}

func (s *scheduler) run(ctx context.Context, now time.Time) (runResult, error) {
	now = now.UTC()
	cutoff := nextInvocation(now)
	events, err := s.events.listBefore(ctx, cutoff, 3*perActionLimit)
	if err != nil {
		return runResult{}, fmt.Errorf("list planned lifecycle events: %w", err)
	}

	result := runResult{}
	registrationCount := 0
	var registrationRecipients []identity
	recipientsLoaded := false
	for _, event := range events {
		if registrationCount >= perActionLimit || !registrationDue(event, cutoff) {
			continue
		}
		if event.RegistrationOpenAt.After(now) {
			opened, err := s.events.openRegistrationNow(ctx, event.EventID, *event.RegistrationOpenAt, now)
			if err != nil {
				return result, fmt.Errorf("open imminent registration: %w", err)
			}
			if !opened {
				// The schedule was edited after the lifecycle read. A later
				// invocation will evaluate the replacement timestamp.
				continue
			}
		}
		registrationCount++
		if !recipientsLoaded {
			registrationRecipients, err = s.recipients.registrationRecipients()
			if err != nil {
				return result, fmt.Errorf("load registration notification recipients: %w", err)
			}
			recipientsLoaded = true
		}
		notified := make(map[string]struct{}, len(event.RegistrationRecipients)+len(registrationRecipients))
		for _, recipient := range event.RegistrationRecipients {
			notified[recipient] = struct{}{}
		}
		pending := make([]identity, 0, len(registrationRecipients))
		for _, recipient := range registrationRecipients {
			key := recipient.key()
			if _, ok := notified[key]; ok {
				continue
			}
			pending = append(pending, recipient)
		}
		deliveryErrors := make([]error, len(pending))
		var deliveries sync.WaitGroup
		for index, recipient := range pending {
			deliveries.Add(1)
			go func() {
				defer deliveries.Done()
				deliveryErrors[index] = s.notifier.sendRegistrationOpening(ctx, event, recipient)
			}()
		}
		deliveries.Wait()
		for index, recipient := range pending {
			key := recipient.key()
			if err := deliveryErrors[index]; err != nil {
				slog.Error("registration_open_notification_failed",
					"event_id", event.EventID,
					"platform", recipient.Platform,
					"platform_user_id", recipient.UserID,
					"error", err,
				)
				continue
			}
			notified[key] = struct{}{}
			event.RegistrationRecipients = append(event.RegistrationRecipients, key)
			if err := s.events.markRegistrationDelivered(ctx, event.EventID, event.RegistrationRecipients, now); err != nil {
				return result, fmt.Errorf("record registration notification delivery: %w", err)
			}
			result.NotificationsDelivered++
		}
		if allNotified(notified, registrationRecipients) {
			if err := s.events.completeRegistration(ctx, event.EventID, now); err != nil {
				return result, fmt.Errorf("complete registration notification: %w", err)
			}
			result.EventsCompleted++
		}
	}

	confirmationCount := 0
	closingCount := 0
	for _, event := range events {
		if confirmationCount < perActionLimit && confirmationDue(event, cutoff) {
			command := scheduledConfirmationCommand(event, now)
			if err := s.publisher.publish(ctx, command); err != nil {
				return result, fmt.Errorf("publish scheduled confirmation command: %w", err)
			}
			confirmationCount++
			result.ConfirmationCommandsEnqueued++
		}
		if closingCount < perActionLimit && closingDue(event, cutoff) {
			command := scheduledClosingCommand(event, now)
			if err := s.publisher.publish(ctx, command); err != nil {
				return result, fmt.Errorf("publish scheduled closing command: %w", err)
			}
			closingCount++
			result.ClosingCommandsEnqueued++
		}
	}
	return result, nil
}

func registrationDue(event plannedEvent, cutoff time.Time) bool {
	return beforeNextInvocation(event.RegistrationOpenAt, cutoff) &&
		event.RegistrationCompleted == nil && event.ArchivedAt == nil && event.Status != "CLOSED"
}

func confirmationDue(event plannedEvent, cutoff time.Time) bool {
	return beforeNextInvocation(event.ConfirmationOpenAt, cutoff) && event.ConfirmationDeadline != nil &&
		event.ArchivedAt == nil && event.Status == "CREATED"
}

func closingDue(event plannedEvent, cutoff time.Time) bool {
	return beforeNextInvocation(event.ScheduledCloseAt, cutoff) && event.ScheduledCloseCompleted == nil &&
		(event.ArchivedAt == nil || event.ScheduledCloseOperation != nil)
}

func allNotified(notified map[string]struct{}, recipients []identity) bool {
	for _, recipient := range recipients {
		if _, ok := notified[recipient.key()]; !ok {
			return false
		}
	}
	return true
}

type orderedCommand struct {
	SchemaVersion  int            `json:"schema_version"`
	OperationID    string         `json:"operation_id"`
	EventID        string         `json:"event_id"`
	Operation      string         `json:"operation"`
	Platform       string         `json:"platform"`
	PlatformUserID int64          `json:"platform_user_id"`
	ParticipantKey *string        `json:"participant_key"`
	IdentityGroup  *string        `json:"identity_group"`
	Payload        map[string]any `json:"payload"`
	ReplyContext   replyContext   `json:"reply_context"`
	CreatedAt      time.Time      `json:"created_at"`
}

type replyContext struct {
	ChatID      *int64   `json:"chat_id"`
	PeerID      *int64   `json:"peer_id"`
	TextSuccess string   `json:"text_success"`
	TextFailure string   `json:"text_failure"`
	Buttons     []button `json:"buttons"`
}

func scheduledConfirmationCommand(event plannedEvent, now time.Time) orderedCommand {
	return orderedCommand{
		SchemaVersion:  1,
		OperationID:    operationID("scheduled-confirmation", event.EventID, *event.ConfirmationOpenAt),
		EventID:        event.EventID,
		Operation:      "SCHEDULED_OPEN_CONFIRMATION",
		Platform:       "system",
		PlatformUserID: 0,
		Payload: map[string]any{
			"deadline": event.ConfirmationDeadline.UTC().Format(time.RFC3339Nano),
		},
		ReplyContext: emptyReplyContext(),
		CreatedAt:    now,
	}
}

func scheduledClosingCommand(event plannedEvent, now time.Time) orderedCommand {
	return orderedCommand{
		SchemaVersion:  1,
		OperationID:    operationID("scheduled-close", event.EventID, *event.ScheduledCloseAt),
		EventID:        event.EventID,
		Operation:      "SCHEDULED_CLOSE_EVENT",
		Platform:       "system",
		PlatformUserID: 0,
		Payload:        map[string]any{},
		ReplyContext:   emptyReplyContext(),
		CreatedAt:      now,
	}
}

func emptyReplyContext() replyContext {
	return replyContext{Buttons: []button{}}
}

func operationID(kind, eventID string, scheduledAt time.Time) string {
	name := fmt.Sprintf("larp-bot:%s:%s:%s", kind, eventID, pythonUTCISO(scheduledAt))
	// RFC 4122 URL namespace, matching Python's uuid.NAMESPACE_URL.
	namespace, _ := hex.DecodeString("6ba7b8119dad11d180b400c04fd430c8")
	digest := sha1.Sum(append(namespace, []byte(name)...))
	value := digest[:16]
	value[6] = (value[6] & 0x0f) | 0x50
	value[8] = (value[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		value[0:4], value[4:6], value[6:8], value[8:10], value[10:16])
}

func pythonUTCISO(value time.Time) string {
	value = value.UTC()
	base := value.Format("2006-01-02T15:04:05")
	if value.Nanosecond() != 0 {
		base += fmt.Sprintf(".%06d", value.Nanosecond()/1000)
	}
	return base + "+00:00"
}

func deduplicateAndSort(identities []identity) []identity {
	unique := make(map[string]identity, len(identities))
	for _, item := range identities {
		unique[item.key()] = item
	}
	keys := make([]string, 0, len(unique))
	for key := range unique {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	result := make([]identity, 0, len(keys))
	for _, key := range keys {
		result = append(result, unique[key])
	}
	return result
}

func marshalCommand(command orderedCommand) (string, error) {
	body, err := json.Marshal(command)
	return string(body), err
}
