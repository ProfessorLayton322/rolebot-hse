package main

import (
	"context"
	"encoding/json"
	"errors"
	"sync"
	"testing"
	"time"
)

type fakeEventRepository struct {
	events           []plannedEvent
	dueBefore        time.Time
	backlogCutoff    *time.Time
	backlogCompleted []string
	marked           map[string][]string
	completed        []string
	opened           []string
	refuseOpen       bool
}

func (repository *fakeEventRepository) completeRegistrationNotificationsBefore(
	_ context.Context,
	openedBefore time.Time,
	completedAt time.Time,
) (int, error) {
	repository.backlogCutoff = timePointer(openedBefore)
	completed := 0
	for index := range repository.events {
		event := &repository.events[index]
		if event.RegistrationOpenAt == nil || event.RegistrationOpenAt.After(openedBefore) ||
			event.RegistrationCompleted != nil {
			continue
		}
		event.RegistrationCompleted = timePointer(completedAt)
		repository.backlogCompleted = append(repository.backlogCompleted, event.EventID)
		completed++
	}
	return completed, nil
}

func (repository *fakeEventRepository) listBefore(
	_ context.Context,
	dueBefore time.Time,
	_ int,
) ([]plannedEvent, error) {
	repository.dueBefore = dueBefore
	return repository.events, nil
}

func (repository *fakeEventRepository) openRegistrationNow(
	_ context.Context,
	eventID string,
	_ time.Time,
	_ time.Time,
) (bool, error) {
	repository.opened = append(repository.opened, eventID)
	return !repository.refuseOpen, nil
}

func (repository *fakeEventRepository) markRegistrationDelivered(
	_ context.Context,
	eventID string,
	recipients []string,
	_ time.Time,
) error {
	if repository.marked == nil {
		repository.marked = make(map[string][]string)
	}
	repository.marked[eventID] = append([]string(nil), recipients...)
	return nil
}

func (repository *fakeEventRepository) completeRegistration(
	_ context.Context,
	eventID string,
	completedAt time.Time,
) error {
	repository.completed = append(repository.completed, eventID)
	for index := range repository.events {
		if repository.events[index].EventID == eventID {
			repository.events[index].RegistrationCompleted = timePointer(completedAt)
		}
	}
	return nil
}

type fakeRecipients struct {
	identities []identity
}

func (recipients fakeRecipients) registrationRecipients() ([]identity, error) {
	return recipients.identities, nil
}

type sentNotification struct {
	eventID   string
	recipient identity
}

type fakeNotifier struct {
	mu   sync.Mutex
	sent []sentNotification
	fail map[string]error
}

func (notifier *fakeNotifier) sendRegistrationOpening(
	_ context.Context,
	event plannedEvent,
	recipient identity,
) error {
	notifier.mu.Lock()
	defer notifier.mu.Unlock()
	notifier.sent = append(notifier.sent, sentNotification{eventID: event.EventID, recipient: recipient})
	return notifier.fail[recipient.key()]
}

type fakePublisher struct {
	commands []orderedCommand
}

func (publisher *fakePublisher) publish(_ context.Context, command orderedCommand) error {
	publisher.commands = append(publisher.commands, command)
	return nil
}

func timePointer(value time.Time) *time.Time {
	return &value
}

func TestDispatchCutoffIsFiveMinutesAfterCurrentTime(t *testing.T) {
	tests := []struct {
		now  time.Time
		want time.Time
	}{
		{
			now:  time.Date(2026, 10, 1, 12, 0, 4, 0, time.UTC),
			want: time.Date(2026, 10, 1, 12, 5, 4, 0, time.UTC),
		},
		{
			now:  time.Date(2026, 10, 1, 12, 55, 59, 0, time.UTC),
			want: time.Date(2026, 10, 1, 13, 0, 59, 0, time.UTC),
		},
		{
			now:  time.Date(2026, 10, 1, 13, 0, 0, 0, time.UTC),
			want: time.Date(2026, 10, 1, 13, 5, 0, 0, time.UTC),
		},
	}
	for _, test := range tests {
		if got := dispatchCutoff(test.now); !got.Equal(test.want) {
			t.Fatalf("dispatchCutoff(%s) = %s, want %s", test.now, got, test.want)
		}
	}
}

func TestSchedulerDispatchesAllActionsWithinFiveMinutesTogether(t *testing.T) {
	now := time.Date(2026, 10, 1, 12, 0, 4, 0, time.UTC)
	registrationAt := time.Date(2026, 10, 1, 12, 4, 0, 0, time.UTC)
	confirmationAt := time.Date(2026, 10, 1, 12, 4, 30, 0, time.UTC)
	deadline := confirmationAt.Add(24 * time.Hour)
	closingAt := time.Date(2026, 10, 1, 12, 5, 3, 999999000, time.UTC)
	boundary := time.Date(2026, 10, 1, 12, 5, 4, 0, time.UTC)
	repository := &fakeEventRepository{events: []plannedEvent{
		{
			EventID:                "registration",
			Name:                   "Game",
			Status:                 "CREATED",
			RegistrationOpenAt:     &registrationAt,
			RegistrationRecipients: []string{"telegram:1"},
		},
		{
			EventID:              "confirmation",
			Status:               "CREATED",
			ConfirmationOpenAt:   &confirmationAt,
			ConfirmationDeadline: &deadline,
		},
		{
			EventID:          "closing",
			Status:           "CONFIRMATION_OPEN",
			ScheduledCloseAt: &closingAt,
		},
		{
			EventID:              "next-boundary",
			Status:               "CREATED",
			ConfirmationOpenAt:   &boundary,
			ConfirmationDeadline: &deadline,
		},
	}}
	notifier := &fakeNotifier{}
	publisher := &fakePublisher{}
	service := scheduler{
		events: repository,
		recipients: fakeRecipients{identities: []identity{
			{Platform: "telegram", UserID: 1},
			{Platform: "vk", UserID: 2},
		}},
		notifier:  notifier,
		publisher: publisher,
	}

	result, err := service.run(context.Background(), now)
	if err != nil {
		t.Fatal(err)
	}
	if want := boundary; !repository.dueBefore.Equal(want) {
		t.Fatalf("queried through %s, want %s", repository.dueBefore, want)
	}
	if result != (runResult{
		EventsCompleted:              1,
		NotificationsDelivered:       1,
		ConfirmationCommandsEnqueued: 1,
		ClosingCommandsEnqueued:      1,
	}) {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(notifier.sent) != 1 || notifier.sent[0].recipient.key() != "vk:2" {
		t.Fatalf("unexpected notifications: %#v", notifier.sent)
	}
	if len(repository.opened) != 1 || repository.opened[0] != "registration" {
		t.Fatalf("imminent registration was not opened: %#v", repository.opened)
	}
	if got := repository.marked["registration"]; len(got) != 2 || got[1] != "vk:2" {
		t.Fatalf("unexpected persisted recipients: %#v", got)
	}
	if len(repository.completed) != 1 || repository.completed[0] != "registration" {
		t.Fatalf("unexpected completed events: %#v", repository.completed)
	}
	if len(publisher.commands) != 2 {
		t.Fatalf("published %d commands, want 2", len(publisher.commands))
	}
	if publisher.commands[0].EventID != "confirmation" || publisher.commands[1].EventID != "closing" {
		t.Fatalf("unexpected commands: %#v", publisher.commands)
	}
}

func TestSchedulerCompletesEveryLegacyRegistrationBeforeSendingCurrentOpenings(t *testing.T) {
	cutoff := time.Date(2026, 10, 2, 16, 15, 0, 0, time.UTC)
	now := cutoff.Add(4 * time.Minute)
	legacyOne := cutoff.Add(-time.Hour)
	legacyTwo := cutoff
	current := cutoff.Add(time.Minute)
	repository := &fakeEventRepository{events: []plannedEvent{
		{EventID: "legacy-one", Name: "Same game", Status: "CREATED", RegistrationOpenAt: &legacyOne},
		{EventID: "legacy-two", Name: "Same game", Status: "CREATED", RegistrationOpenAt: &legacyTwo},
		{EventID: "current", Name: "Current game", Status: "CREATED", RegistrationOpenAt: &current},
	}}
	notifier := &fakeNotifier{}
	service := scheduler{
		events:                    repository,
		recipients:                fakeRecipients{identities: []identity{{Platform: "telegram", UserID: 1}}},
		notifier:                  notifier,
		publisher:                 &fakePublisher{},
		registrationBacklogCutoff: &cutoff,
	}

	result, err := service.run(context.Background(), now)
	if err != nil {
		t.Fatal(err)
	}
	if result.BacklogEventsCompleted != 2 || result.EventsCompleted != 1 || result.NotificationsDelivered != 1 {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(repository.backlogCompleted) != 2 || repository.backlogCompleted[0] != "legacy-one" ||
		repository.backlogCompleted[1] != "legacy-two" {
		t.Fatalf("legacy backlog was not fully completed: %#v", repository.backlogCompleted)
	}
	if len(notifier.sent) != 1 || notifier.sent[0].eventID != "current" {
		t.Fatalf("unexpected notifications: %#v", notifier.sent)
	}

	repeated, err := service.run(context.Background(), now.Add(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if repeated != (runResult{}) || len(notifier.sent) != 1 {
		t.Fatalf("completed backlog ran again: result=%#v sent=%#v", repeated, notifier.sent)
	}
}

func TestChangedRegistrationScheduleIsNotOpenedOrNotified(t *testing.T) {
	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	opensAt := now.Add(time.Minute)
	repository := &fakeEventRepository{refuseOpen: true, events: []plannedEvent{{
		EventID:            "registration",
		Name:               "Game",
		Status:             "CREATED",
		RegistrationOpenAt: &opensAt,
	}}}
	notifier := &fakeNotifier{}
	service := scheduler{
		events:     repository,
		recipients: fakeRecipients{identities: []identity{{Platform: "telegram", UserID: 1}}},
		notifier:   notifier,
		publisher:  &fakePublisher{},
	}

	result, err := service.run(context.Background(), now)
	if err != nil {
		t.Fatal(err)
	}
	if result != (runResult{}) || len(notifier.sent) != 0 || len(repository.completed) != 0 {
		t.Fatalf("stale schedule was processed: result=%#v sent=%#v", result, notifier.sent)
	}
}

func TestFailedNotificationStaysPending(t *testing.T) {
	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	opensAt := now.Add(time.Minute)
	repository := &fakeEventRepository{events: []plannedEvent{{
		EventID:            "registration",
		Name:               "Game",
		Status:             "CREATED",
		RegistrationOpenAt: &opensAt,
	}}}
	notifier := &fakeNotifier{fail: map[string]error{"vk:2": errors.New("unavailable")}}
	service := scheduler{
		events: repository,
		recipients: fakeRecipients{identities: []identity{
			{Platform: "telegram", UserID: 1},
			{Platform: "vk", UserID: 2},
		}},
		notifier:  notifier,
		publisher: &fakePublisher{},
	}

	result, err := service.run(context.Background(), now)
	if err != nil {
		t.Fatal(err)
	}
	if result.EventsCompleted != 0 || result.NotificationsDelivered != 1 {
		t.Fatalf("unexpected result: %#v", result)
	}
	if len(repository.completed) != 0 {
		t.Fatal("event was completed while one recipient remained pending")
	}
}

func TestOperationIDMatchesExistingPythonUUID5(t *testing.T) {
	scheduledAt := time.Date(2026, 10, 1, 12, 1, 30, 123000000, time.UTC)
	if got, want := operationID("scheduled-confirmation", "event-a", scheduledAt),
		"ec19071b-c0e9-5a3b-b49b-50b00621c718"; got != want {
		t.Fatalf("operationID() = %q, want %q", got, want)
	}
	if got, want := operationID("scheduled-close", "event-a", scheduledAt),
		"54adb2cf-ab1d-51b2-bad3-58e774f0e6f2"; got != want {
		t.Fatalf("operationID() = %q, want %q", got, want)
	}
}

func TestRuntimeRecipientsAreDeduplicatedAndTelegramCallbackFitsLimit(t *testing.T) {
	config := runtimeConfiguration{Values: map[string]string{
		"TG_ADMIN_IDS":      "[1]",
		"VK_ADMIN_IDS":      "[2]",
		"TG_GAMEMASTER_IDS": "[1,3]",
		"VK_GAMEMASTER_IDS": "[2]",
	}}
	recipients, err := config.registrationRecipients()
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"telegram:1", "telegram:3", "vk:2"}
	if len(recipients) != len(want) {
		t.Fatalf("unexpected recipients: %#v", recipients)
	}
	for index, recipient := range recipients {
		if recipient.key() != want[index] {
			t.Fatalf("recipient %d = %q, want %q", index, recipient.key(), want[index])
		}
	}
	if size := len([]byte(mainMenuValue)); size < 1 || size > 64 {
		t.Fatalf("main-menu callback_data uses %d bytes", size)
	}
}

func TestScheduledCommandJSONRetainsWorkerContract(t *testing.T) {
	now := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	opensAt := now.Add(time.Minute)
	deadline := now.Add(24 * time.Hour)
	command := scheduledConfirmationCommand(plannedEvent{
		EventID:              "event-a",
		ConfirmationOpenAt:   &opensAt,
		ConfirmationDeadline: &deadline,
	}, now)
	body, err := marshalCommand(command)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal([]byte(body), &decoded); err != nil {
		t.Fatal(err)
	}
	if decoded["schema_version"] != float64(1) || decoded["operation"] != "SCHEDULED_OPEN_CONFIRMATION" ||
		decoded["platform"] != "system" {
		t.Fatalf("unexpected command JSON: %s", body)
	}
}
