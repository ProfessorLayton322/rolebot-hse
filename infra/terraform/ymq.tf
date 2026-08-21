resource "yandex_message_queue" "registration_commands" {
  name                        = "registration-commands.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  visibility_timeout_seconds  = 180
  receive_wait_time_seconds   = 20
  message_retention_seconds   = 1209600
  max_message_size            = 262144
  access_key                  = yandex_iam_service_account_static_access_key.ymq_client.access_key
  secret_key                  = yandex_iam_service_account_static_access_key.ymq_client.secret_key

  depends_on = [time_sleep.ymq_writer_ready]
}

resource "yandex_message_queue" "worker_kicks" {
  name                       = "registration-worker-kicks"
  visibility_timeout_seconds = 90
  receive_wait_time_seconds  = 10
  message_retention_seconds  = 86400
  max_message_size           = 16384
  access_key                 = yandex_iam_service_account_static_access_key.ymq_client.access_key
  secret_key                 = yandex_iam_service_account_static_access_key.ymq_client.secret_key

  depends_on = [time_sleep.ymq_writer_ready]
}

# Deliberately attached ONLY to the standard kick queue. Yandex's native trigger
# rejects FIFO queues; the worker receives authoritative commands itself.
resource "yandex_function_trigger" "worker_kicks" {
  name        = "${var.project_name}-worker-kicks"
  description = "Wake the FIFO drainer from the standard kick queue"
  folder_id   = var.yandex_folder_id

  function {
    id                 = yandex_function.ordered_worker.id
    service_account_id = yandex_iam_service_account.trigger.id
  }

  message_queue {
    queue_id           = yandex_message_queue.worker_kicks.arn
    service_account_id = yandex_iam_service_account.trigger.id
    batch_size         = "1"
    batch_cutoff       = "1"
    visibility_timeout = "80"
  }

  depends_on = [
    yandex_resourcemanager_folder_iam_member.runtime,
    yandex_function_iam_member.worker_from_trigger,
  ]
}
