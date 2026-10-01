package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestTelegramNotificationKeepsProductionSizedIDsAndCallbackWithinLimit(t *testing.T) {
	const (
		eventID = "12345678-1234-1234-1234-123456789abc"
		secret  = "test-hmac-secret"
	)
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, err := io.ReadAll(request.Body)
		if err != nil {
			t.Fatal(err)
		}
		requestID := request.Header.Get("X-Request-Id")
		wantRequestID := "registration-open:" + eventID + ":telegram:9223372036854775807"
		if requestID != wantRequestID {
			t.Errorf("request ID = %q, want %q", requestID, wantRequestID)
		}
		wantSignature := signRequest(secret, request.Header.Get("X-Timestamp"), requestID, telegramPath, body)
		if request.Header.Get("X-Signature") != wantSignature {
			t.Error("Telegram egress signature does not match the final request body")
		}
		var envelope struct {
			Payload struct {
				Text        string `json:"text"`
				ReplyMarkup struct {
					InlineKeyboard [][]struct {
						CallbackData string `json:"callback_data"`
					} `json:"inline_keyboard"`
				} `json:"reply_markup"`
			} `json:"payload"`
		}
		if err := json.Unmarshal(body, &envelope); err != nil {
			t.Fatal(err)
		}
		if envelope.Payload.Text != "Регистрация на игру «Очень длинная игра Ёж» (01.10.26) открыта." {
			t.Errorf("unexpected non-ASCII notification: %q", envelope.Payload.Text)
		}
		callback := envelope.Payload.ReplyMarkup.InlineKeyboard[0][0].CallbackData
		if size := len([]byte(callback)); size < 1 || size > 64 {
			t.Errorf("callback_data uses %d bytes: %q", size, callback)
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(writer, `{"ok":true,"result":{"message_id":1}}`)
	}))
	defer server.Close()

	gameDate := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	notifier := directNotifier{
		client:         server.Client(),
		telegramURL:    server.URL,
		telegramSecret: secret,
	}
	err := notifier.sendRegistrationOpening(context.Background(), plannedEvent{
		EventID:  eventID,
		Name:     "Очень длинная игра Ёж",
		GameDate: &gameDate,
	}, identity{Platform: "telegram", UserID: 9223372036854775807})
	if err != nil {
		t.Fatal(err)
	}
}
