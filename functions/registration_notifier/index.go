package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"
)

var now = time.Now

// Handler is the Yandex Cloud Functions timer entry point.
func Handler(ctx context.Context, _ []byte) ([]byte, error) {
	env, err := loadEnvironment()
	if err != nil {
		return nil, err
	}
	client := &http.Client{Timeout: 10 * time.Second}
	iamToken, err := fetchIAMToken(ctx, client, metadataTokenURL)
	if err != nil {
		return nil, fmt.Errorf("fetch IAM token: %w", err)
	}
	runtimeConfig, err := fetchRuntimeConfiguration(ctx, client, env, iamToken, tokenExchangeURL)
	if err != nil {
		return nil, fmt.Errorf("fetch runtime configuration: %w", err)
	}
	driver, err := openYDB(ctx, env.YDBEndpoint, env.YDBDatabase, iamToken)
	if err != nil {
		return nil, err
	}
	defer func() { _ = driver.Close(ctx) }()
	notifier, err := newDirectNotifier(client, env, runtimeConfig)
	if err != nil {
		return nil, fmt.Errorf("configure notification transport: %w", err)
	}
	publisher, err := newYMQPublisher(ctx, env, runtimeConfig)
	if err != nil {
		return nil, fmt.Errorf("configure command publisher: %w", err)
	}
	service := scheduler{
		events:     &ydbEventRepository{driver: driver},
		recipients: runtimeConfig,
		notifier:   notifier,
		publisher:  publisher,
	}
	result, err := service.run(ctx, now().UTC())
	if err != nil {
		return nil, err
	}
	return json.Marshal(result)
}
