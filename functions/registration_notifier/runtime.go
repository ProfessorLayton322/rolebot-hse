package main

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

const (
	metadataTokenURL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
	tokenExchangeURL = "https://auth.yandex.cloud/oauth/token"
	telegramPath     = "/telegram/send"
	vkAPIURL         = "https://api.vk.com/method/messages.send"
	mainMenuLabel    = "В главное меню"
	mainMenuValue    = "Главное меню"
)

type environment struct {
	YDBEndpoint             string
	YDBDatabase             string
	YMQEndpoint             string
	YMQFIFOURL              string
	YMQKickURL              string
	RuntimeConfigURL        string
	RuntimeConfigAudience   string
	RuntimeServiceAccountID string
	TelegramEgressURL       string
}

func yandexRuntimeEnabled() bool {
	mode, modeConfigured := os.LookupEnv("APPLICATION_MODE")
	mutations, mutationsConfigured := os.LookupEnv("MUTATIONS_ENABLED")
	if !modeConfigured && !mutationsConfigured {
		return true
	}
	return mode == "active" && mutations == "true"
}

func loadEnvironment() (environment, error) {
	env := environment{
		YDBEndpoint:             os.Getenv("YDB_ENDPOINT"),
		YDBDatabase:             os.Getenv("YDB_DATABASE"),
		YMQEndpoint:             os.Getenv("YMQ_ENDPOINT"),
		YMQFIFOURL:              os.Getenv("YMQ_FIFO_URL"),
		YMQKickURL:              os.Getenv("YMQ_KICK_URL"),
		RuntimeConfigURL:        os.Getenv("RUNTIME_CONFIG_URL"),
		RuntimeConfigAudience:   os.Getenv("RUNTIME_CONFIG_AUDIENCE"),
		RuntimeServiceAccountID: os.Getenv("RUNTIME_SERVICE_ACCOUNT_ID"),
		TelegramEgressURL:       os.Getenv("TELEGRAM_EGRESS_URL"),
	}
	if env.YDBEndpoint == "" || env.YDBDatabase == "" || env.YMQEndpoint == "" ||
		env.YMQFIFOURL == "" || env.YMQKickURL == "" || env.RuntimeConfigURL == "" ||
		env.RuntimeConfigAudience == "" || env.RuntimeServiceAccountID == "" || env.TelegramEgressURL == "" {
		return environment{}, errors.New("registration notifier environment is incomplete")
	}
	return env, nil
}

type metadataToken struct {
	AccessToken string `json:"access_token"`
}

type exchangedToken struct {
	AccessToken string `json:"access_token"`
	IDToken     string `json:"id_token"`
}

type runtimeConfiguration struct {
	Values map[string]string `json:"values"`
}

func readHTTPResponse(response *http.Response) ([]byte, error) {
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("%s %s returned HTTP %d: %s",
			response.Request.Method, response.Request.URL, response.StatusCode, body)
	}
	return body, nil
}

func fetchIAMToken(ctx context.Context, client *http.Client, metadataURL string) (string, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, metadataURL, nil)
	if err != nil {
		return "", err
	}
	request.Header.Set("Metadata-Flavor", "Google")
	response, err := client.Do(request)
	if err != nil {
		return "", err
	}
	body, err := readHTTPResponse(response)
	if err != nil {
		return "", err
	}
	var token metadataToken
	if err := json.Unmarshal(body, &token); err != nil || token.AccessToken == "" {
		return "", errors.New("metadata service returned no IAM token")
	}
	return token.AccessToken, nil
}

func fetchRuntimeConfiguration(
	ctx context.Context,
	client *http.Client,
	env environment,
	iamToken string,
	tokenURL string,
) (runtimeConfiguration, error) {
	form := url.Values{
		"actor_token":          {iamToken},
		"actor_token_type":     {"urn:ietf:params:oauth:token-type:access_token"},
		"audience":             {env.RuntimeConfigAudience},
		"grant_type":           {"urn:ietf:params:oauth:grant-type:token-exchange"},
		"requested_token_type": {"urn:ietf:params:oauth:token-type:id_token"},
		"subject_token":        {env.RuntimeServiceAccountID},
		"subject_token_type":   {"urn:yandex-cloud:token-type:subject_id"},
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return runtimeConfiguration{}, err
	}
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response, err := client.Do(request)
	if err != nil {
		return runtimeConfiguration{}, err
	}
	body, err := readHTTPResponse(response)
	if err != nil {
		return runtimeConfiguration{}, err
	}
	var exchanged exchangedToken
	if err := json.Unmarshal(body, &exchanged); err != nil {
		return runtimeConfiguration{}, err
	}
	identityToken := exchanged.AccessToken
	if identityToken == "" {
		identityToken = exchanged.IDToken
	}
	if identityToken == "" {
		return runtimeConfiguration{}, errors.New("identity exchange returned no token")
	}

	request, err = http.NewRequestWithContext(ctx, http.MethodGet, env.RuntimeConfigURL, nil)
	if err != nil {
		return runtimeConfiguration{}, err
	}
	request.Header.Set("Authorization", "Bearer "+identityToken)
	response, err = client.Do(request)
	if err != nil {
		return runtimeConfiguration{}, err
	}
	body, err = readHTTPResponse(response)
	if err != nil {
		return runtimeConfiguration{}, err
	}
	var config runtimeConfiguration
	if err := json.Unmarshal(body, &config); err != nil {
		return runtimeConfiguration{}, err
	}
	if len(config.Values) == 0 {
		return runtimeConfiguration{}, errors.New("runtime config endpoint returned no values")
	}
	return config, nil
}

func (config runtimeConfiguration) required(key string) (string, error) {
	value := config.Values[key]
	if value == "" {
		return "", fmt.Errorf("runtime config entry is missing: %s", key)
	}
	return value, nil
}

func (config runtimeConfiguration) registrationRecipients() ([]identity, error) {
	var identities []identity
	for _, item := range []struct {
		key      string
		platform string
	}{
		{key: "TG_ADMIN_IDS", platform: "telegram"},
		{key: "VK_ADMIN_IDS", platform: "vk"},
		{key: "TG_GAMEMASTER_IDS", platform: "telegram"},
		{key: "VK_GAMEMASTER_IDS", platform: "vk"},
	} {
		raw, err := config.required(item.key)
		if err != nil {
			return nil, err
		}
		var ids []int64
		if err := json.Unmarshal([]byte(raw), &ids); err != nil {
			return nil, fmt.Errorf("%s must be a JSON numeric array: %w", item.key, err)
		}
		for _, id := range ids {
			if id <= 0 {
				return nil, fmt.Errorf("%s must contain positive IDs", item.key)
			}
			identities = append(identities, identity{Platform: item.platform, UserID: id})
		}
	}
	return deduplicateAndSort(identities), nil
}

type button struct {
	Label string `json:"label"`
	Value string `json:"value"`
}

type directNotifier struct {
	client         *http.Client
	telegramURL    string
	telegramSecret string
	vkURL          string
	vkAccessToken  string
}

func newDirectNotifier(
	client *http.Client,
	env environment,
	config runtimeConfiguration,
) (*directNotifier, error) {
	secretName := "YANDEX_TO_CF_EGRESS_HMAC_SECRET"
	if config.Values["AWS_TO_CF_EGRESS_HMAC_SECRET"] != "" {
		secretName = "AWS_TO_CF_EGRESS_HMAC_SECRET"
	}
	telegramSecret, err := config.required(secretName)
	if err != nil {
		return nil, err
	}
	vkAccessToken, err := config.required("VK_ACCESS_TOKEN")
	if err != nil {
		return nil, err
	}
	return &directNotifier{
		client:         client,
		telegramURL:    strings.TrimRight(env.TelegramEgressURL, "/") + telegramPath,
		telegramSecret: telegramSecret,
		vkURL:          vkAPIURL,
		vkAccessToken:  vkAccessToken,
	}, nil
}

func (notifier *directNotifier) sendRegistrationOpening(
	ctx context.Context,
	event plannedEvent,
	recipient identity,
) error {
	requestID := fmt.Sprintf("registration-open:%s:%s:%d", event.EventID, recipient.Platform, recipient.UserID)
	text := fmt.Sprintf("Регистрация на игру %s открыта.", eventReference(event))
	buttons := []button{{Label: mainMenuLabel, Value: mainMenuValue}}
	switch recipient.Platform {
	case "telegram":
		return notifier.sendTelegram(ctx, recipient.UserID, requestID, text, buttons)
	case "vk":
		return notifier.sendVK(ctx, recipient.UserID, requestID, text, buttons)
	default:
		return fmt.Errorf("unsupported notification platform: %s", recipient.Platform)
	}
}

func eventReference(event plannedEvent) string {
	suffix := ""
	if event.GameDate != nil {
		suffix = " (" + event.GameDate.UTC().Format("02.01.06") + ")"
	}
	return "«" + event.Name + "»" + suffix
}

func (notifier *directNotifier) sendTelegram(
	ctx context.Context,
	userID int64,
	requestID string,
	text string,
	buttons []button,
) error {
	keyboard := make([][]map[string]string, 0, len(buttons))
	for _, item := range buttons {
		keyboard = append(keyboard, []map[string]string{{
			"text":          item.Label,
			"callback_data": item.Value,
		}})
	}
	body, err := json.Marshal(map[string]any{
		"request_id": requestID,
		"method":     "sendMessage",
		"payload": map[string]any{
			"chat_id": userID,
			"text":    text,
			"reply_markup": map[string]any{
				"inline_keyboard": keyboard,
			},
		},
	})
	if err != nil {
		return err
	}
	timestamp := strconv.FormatInt(time.Now().Unix(), 10)
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, notifier.telegramURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("X-Request-Id", requestID)
	request.Header.Set("X-Timestamp", timestamp)
	request.Header.Set("X-Signature", signRequest(notifier.telegramSecret, timestamp, requestID, telegramPath, body))
	response, err := notifier.client.Do(request)
	if err != nil {
		return err
	}
	responseBody, err := readHTTPResponse(response)
	if err != nil {
		return err
	}
	var payload struct {
		OK bool `json:"ok"`
	}
	if err := json.Unmarshal(responseBody, &payload); err != nil || !payload.OK {
		return errors.New("Telegram API returned an invalid success response")
	}
	return nil
}

func signRequest(secret, timestamp, requestID, path string, body []byte) string {
	bodyDigest := sha256.Sum256(body)
	canonical := strings.Join([]string{
		timestamp,
		requestID,
		http.MethodPost,
		path,
		hex.EncodeToString(bodyDigest[:]),
	}, "\n")
	signature := hmac.New(sha256.New, []byte(secret))
	_, _ = signature.Write([]byte(canonical))
	return hex.EncodeToString(signature.Sum(nil))
}

type vkResponse struct {
	Error *struct {
		Code    int    `json:"error_code"`
		Message string `json:"error_msg"`
	} `json:"error"`
}

func (notifier *directNotifier) sendVK(
	ctx context.Context,
	userID int64,
	requestID string,
	text string,
	buttons []button,
) error {
	keyboardButtons := make([][]map[string]any, 0, len(buttons))
	for _, item := range buttons {
		payload, err := json.Marshal(map[string]string{"value": item.Value})
		if err != nil {
			return err
		}
		keyboardButtons = append(keyboardButtons, []map[string]any{{
			"action": map[string]string{
				"type":    "text",
				"label":   item.Label,
				"payload": string(payload),
			},
			"color": "primary",
		}})
	}
	keyboard, err := json.Marshal(map[string]any{
		"one_time": false,
		"inline":   false,
		"buttons":  keyboardButtons,
	})
	if err != nil {
		return err
	}
	digest := sha256.Sum256([]byte(requestID))
	values := url.Values{
		"access_token": {notifier.vkAccessToken},
		"v":            {"5.199"},
		"peer_id":      {strconv.FormatInt(userID, 10)},
		"random_id":    {strconv.FormatUint(uint64(digest[0])<<24|uint64(digest[1])<<16|uint64(digest[2])<<8|uint64(digest[3]), 10)},
		"message":      {text},
		"keyboard":     {string(keyboard)},
	}
	payload, err := notifier.postVK(ctx, values)
	if err != nil {
		return err
	}
	if payload.Error != nil && payload.Error.Code == 912 {
		values.Del("keyboard")
		values.Set("message", vkTextKeyboard(text, buttons))
		payload, err = notifier.postVK(ctx, values)
		if err != nil {
			return err
		}
	}
	if payload.Error != nil {
		return fmt.Errorf("VK API error code %d: %s", payload.Error.Code, payload.Error.Message)
	}
	return nil
}

func (notifier *directNotifier) postVK(ctx context.Context, values url.Values) (vkResponse, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, notifier.vkURL, strings.NewReader(values.Encode()))
	if err != nil {
		return vkResponse{}, err
	}
	request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	response, err := notifier.client.Do(request)
	if err != nil {
		return vkResponse{}, err
	}
	body, err := readHTTPResponse(response)
	if err != nil {
		return vkResponse{}, err
	}
	var payload vkResponse
	if err := json.Unmarshal(body, &payload); err != nil {
		return vkResponse{}, err
	}
	return payload, nil
}

func vkTextKeyboard(text string, buttons []button) string {
	lines := []string{text, "", "Доступные команды (отправьте нужную строку сообщением):"}
	for _, item := range buttons {
		lines = append(lines, "• "+item.Label)
		if item.Value != item.Label {
			lines = append(lines, "  Команда: "+item.Value)
		}
	}
	return strings.Join(lines, "\n")
}
