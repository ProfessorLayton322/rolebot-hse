//go:build !aws

package main

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
)

type sqsSender interface {
	SendMessage(context.Context, *sqs.SendMessageInput, ...func(*sqs.Options)) (*sqs.SendMessageOutput, error)
}

type ymqPublisher struct {
	client  sqsSender
	fifoURL string
	kickURL string
}

func newYMQPublisher(
	ctx context.Context,
	env environment,
	runtimeConfig runtimeConfiguration,
) (*ymqPublisher, error) {
	accessKey, err := runtimeConfig.required("YMQ_ACCESS_KEY_ID")
	if err != nil {
		return nil, err
	}
	secretKey, err := runtimeConfig.required("YMQ_SECRET_ACCESS_KEY")
	if err != nil {
		return nil, err
	}
	awsConfig, err := config.LoadDefaultConfig(ctx,
		config.WithRegion("ru-central1"),
		config.WithCredentialsProvider(credentials.NewStaticCredentialsProvider(accessKey, secretKey, "")),
	)
	if err != nil {
		return nil, err
	}
	client := sqs.NewFromConfig(awsConfig, func(options *sqs.Options) {
		options.BaseEndpoint = aws.String(env.YMQEndpoint)
	})
	return &ymqPublisher{client: client, fifoURL: env.YMQFIFOURL, kickURL: env.YMQKickURL}, nil
}

func (publisher *ymqPublisher) publish(ctx context.Context, command orderedCommand) error {
	body, err := marshalCommand(command)
	if err != nil {
		return err
	}
	if _, err := publisher.client.SendMessage(ctx, &sqs.SendMessageInput{
		QueueUrl:               aws.String(publisher.fifoURL),
		MessageBody:            aws.String(body),
		MessageGroupId:         aws.String(command.EventID),
		MessageDeduplicationId: aws.String(command.OperationID),
	}); err != nil {
		return fmt.Errorf("send FIFO message: %w", err)
	}
	kickBody, err := json.Marshal(map[string]any{
		"schema_version": 1,
		"event_id":       command.EventID,
	})
	if err != nil {
		return err
	}
	if _, err := publisher.client.SendMessage(ctx, &sqs.SendMessageInput{
		QueueUrl:    aws.String(publisher.kickURL),
		MessageBody: aws.String(string(kickBody)),
	}); err != nil {
		return fmt.Errorf("send worker kick: %w", err)
	}
	return nil
}
