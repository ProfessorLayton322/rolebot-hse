package main

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"github.com/ydb-platform/ydb-go-sdk/v3"
	"github.com/ydb-platform/ydb-go-sdk/v3/query"
)

type ydbEventRepository struct {
	driver *ydb.Driver
}

func openYDB(ctx context.Context, endpoint, database, iamToken string) (*ydb.Driver, error) {
	dsn := strings.TrimRight(endpoint, "/") + "/" + strings.TrimLeft(database, "/")
	driver, err := ydb.Open(ctx, dsn, ydb.WithAccessTokenCredentials(iamToken))
	if err != nil {
		return nil, fmt.Errorf("open YDB: %w", err)
	}
	return driver, nil
}

func (repository *ydbEventRepository) listBefore(
	ctx context.Context,
	dueBefore time.Time,
	limit int,
) ([]plannedEvent, error) {
	result, err := repository.driver.Query().QueryResultSet(ctx, `
DECLARE $due_before AS Timestamp;
DECLARE $created_status AS Utf8;
DECLARE $closed_status AS Utf8;
DECLARE $limit AS Uint64;

SELECT event_id, name, game_date, status,
       registration_open_at, registration_open_notified_recipients_json,
       registration_open_notification_completed_at,
       confirmation_open_at, confirmation_deadline,
       scheduled_close_at, scheduled_close_operation_id,
       scheduled_close_completed_at, archived_at
FROM events
WHERE (
    registration_open_at IS NOT NULL
    AND registration_open_at < $due_before
    AND registration_open_notification_completed_at IS NULL
    AND archived_at IS NULL
    AND status != $closed_status
) OR (
    confirmation_open_at IS NOT NULL
    AND confirmation_open_at < $due_before
    AND confirmation_deadline IS NOT NULL
    AND archived_at IS NULL
    AND status = $created_status
) OR (
    scheduled_close_at IS NOT NULL
    AND scheduled_close_at < $due_before
    AND scheduled_close_completed_at IS NULL
    AND (archived_at IS NULL OR scheduled_close_operation_id IS NOT NULL)
)
ORDER BY event_id
LIMIT $limit;
`,
		query.WithParameters(ydb.ParamsBuilder().
			Param("$due_before").Timestamp(dueBefore.UTC()).
			Param("$created_status").Text("CREATED").
			Param("$closed_status").Text("CLOSED").
			Param("$limit").Uint64(uint64(limit)).
			Build()),
		query.WithTxControl(query.SnapshotReadOnlyTxControl()),
	)
	if err != nil {
		return nil, err
	}
	defer func() { _ = result.Close(ctx) }()

	events := make([]plannedEvent, 0)
	for row, rowErr := range result.Rows(ctx) {
		if rowErr != nil {
			return nil, rowErr
		}
		var (
			event          plannedEvent
			recipientsJSON *string
		)
		if err := row.ScanNamed(
			query.Named("event_id", &event.EventID),
			query.Named("name", &event.Name),
			query.Named("game_date", &event.GameDate),
			query.Named("status", &event.Status),
			query.Named("registration_open_at", &event.RegistrationOpenAt),
			query.Named("registration_open_notified_recipients_json", &recipientsJSON),
			query.Named("registration_open_notification_completed_at", &event.RegistrationCompleted),
			query.Named("confirmation_open_at", &event.ConfirmationOpenAt),
			query.Named("confirmation_deadline", &event.ConfirmationDeadline),
			query.Named("scheduled_close_at", &event.ScheduledCloseAt),
			query.Named("scheduled_close_operation_id", &event.ScheduledCloseOperation),
			query.Named("scheduled_close_completed_at", &event.ScheduledCloseCompleted),
			query.Named("archived_at", &event.ArchivedAt),
		); err != nil {
			return nil, err
		}
		if recipientsJSON != nil && *recipientsJSON != "" {
			if err := json.Unmarshal([]byte(*recipientsJSON), &event.RegistrationRecipients); err != nil {
				return nil, fmt.Errorf("decode registration recipients for %s: %w", event.EventID, err)
			}
		}
		events = append(events, event)
	}
	return events, nil
}

func (repository *ydbEventRepository) openRegistrationNow(
	ctx context.Context,
	eventID string,
	scheduledAt time.Time,
	openedAt time.Time,
) (bool, error) {
	result, err := repository.driver.Query().QueryResultSet(ctx, `
DECLARE $event_id AS Utf8;
DECLARE $scheduled_at AS Timestamp;
DECLARE $opened_at AS Timestamp;
DECLARE $closed_status AS Utf8;

UPDATE events
SET registration_open_at = $opened_at,
    updated_at = $opened_at
WHERE event_id = $event_id
  AND registration_open_at = $scheduled_at
  AND registration_open_at > $opened_at
  AND registration_open_notification_completed_at IS NULL
  AND archived_at IS NULL
  AND status != $closed_status
RETURNING event_id;
`,
		query.WithParameters(ydb.ParamsBuilder().
			Param("$event_id").Text(eventID).
			Param("$scheduled_at").Timestamp(scheduledAt.UTC()).
			Param("$opened_at").Timestamp(openedAt.UTC()).
			Param("$closed_status").Text("CLOSED").
			Build()),
		query.WithTxControl(query.SerializableReadWriteTxControl()),
	)
	if err != nil {
		return false, err
	}
	defer func() { _ = result.Close(ctx) }()
	for _, rowErr := range result.Rows(ctx) {
		if rowErr != nil {
			return false, rowErr
		}
		return true, nil
	}
	return false, nil
}

func (repository *ydbEventRepository) markRegistrationDelivered(
	ctx context.Context,
	eventID string,
	recipients []string,
	updatedAt time.Time,
) error {
	encoded, err := json.Marshal(recipients)
	if err != nil {
		return err
	}
	return repository.driver.Query().Exec(ctx, `
DECLARE $event_id AS Utf8;
DECLARE $notified_recipients_json AS Utf8;
DECLARE $updated_at AS Timestamp;

UPDATE events
SET registration_open_notified_recipients_json = $notified_recipients_json,
    updated_at = $updated_at
WHERE event_id = $event_id
  AND registration_open_notification_completed_at IS NULL;
`,
		query.WithParameters(ydb.ParamsBuilder().
			Param("$event_id").Text(eventID).
			Param("$notified_recipients_json").Text(string(encoded)).
			Param("$updated_at").Timestamp(updatedAt.UTC()).
			Build()),
		query.WithTxControl(query.SerializableReadWriteTxControl()),
	)
}

func (repository *ydbEventRepository) completeRegistration(
	ctx context.Context,
	eventID string,
	completedAt time.Time,
) error {
	return repository.driver.Query().Exec(ctx, `
DECLARE $event_id AS Utf8;
DECLARE $completed_at AS Timestamp;

UPDATE events
SET registration_open_notification_completed_at = $completed_at,
    updated_at = $completed_at
WHERE event_id = $event_id
  AND registration_open_notification_completed_at IS NULL;
`,
		query.WithParameters(ydb.ParamsBuilder().
			Param("$event_id").Text(eventID).
			Param("$completed_at").Timestamp(completedAt.UTC()).
			Build()),
		query.WithTxControl(query.SerializableReadWriteTxControl()),
	)
}
