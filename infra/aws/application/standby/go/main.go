package main

import (
	"bytes"
	"fmt"
	"io"
	"net/http"
	"os"
)

const standbyResponse = `{"statusCode":503,"headers":{"content-type":"application/json"},"body":"{\"status\":\"standby\",\"mutations_enabled\":false}"}`

func main() {
	runtimeAPI := os.Getenv("AWS_LAMBDA_RUNTIME_API")
	if runtimeAPI == "" {
		fmt.Fprintln(os.Stderr, "AWS_LAMBDA_RUNTIME_API is not set")
		os.Exit(1)
	}

	client := &http.Client{}
	for {
		request, err := http.Get("http://" + runtimeAPI + "/2018-06-01/runtime/invocation/next")
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			continue
		}
		_, _ = io.Copy(io.Discard, request.Body)
		_ = request.Body.Close()

		requestID := request.Header.Get("Lambda-Runtime-Aws-Request-Id")
		responseURL := "http://" + runtimeAPI + "/2018-06-01/runtime/invocation/" + requestID + "/response"
		response, err := client.Post(responseURL, "application/json", bytes.NewBufferString(standbyResponse))
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			continue
		}
		_, _ = io.Copy(io.Discard, response.Body)
		_ = response.Body.Close()
	}
}
