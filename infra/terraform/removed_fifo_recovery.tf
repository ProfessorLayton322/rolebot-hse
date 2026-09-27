# The failed DLQ rollout created these empty queues, but could not attach
# redrive policies. The runtime YMQ key cannot delete queues (ymq.admin is
# required). Forget the unused resources so the rollback does not fail at
# deletion or grant administrative permissions to the runtime identity.
removed {
  from = yandex_message_queue.registration_command_failures
  lifecycle {
    destroy = false
  }
}

removed {
  from = yandex_message_queue.worker_kick_failures
  lifecycle {
    destroy = false
  }
}
