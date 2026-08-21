resource "yandex_api_gateway" "bot" {
  name        = "${var.project_name}-webhooks"
  description = "Authenticated Telegram transport and VK Callback API"
  folder_id   = var.yandex_folder_id
  labels      = local.labels

  spec = <<-YAML
    openapi: "3.0.0"
    x-yc-apigateway:
      smartWebSecurity:
        securityProfileId: ${yandex_sws_security_profile.api_gateway.id}
    info:
      title: LARP bot callbacks
      version: "1.0.0"
    paths:
      /webhooks/telegram:
        post:
          x-yc-apigateway-integration:
            type: cloud_functions
            function_id: ${yandex_function.gateway.id}
            tag: production
            service_account_id: ${yandex_iam_service_account.api_gateway.id}
            payload_format_version: "1.0"
          responses:
            "200":
              description: Telegram delivery contract
      /webhooks/vk:
        post:
          x-yc-apigateway-integration:
            type: cloud_functions
            function_id: ${yandex_function.gateway.id}
            tag: production
            service_account_id: ${yandex_iam_service_account.api_gateway.id}
            payload_format_version: "1.0"
          responses:
            "200":
              description: VK callback acknowledgement
  YAML

  depends_on = [
    yandex_function_iam_member.gateway_from_api_gateway,
    yandex_sws_security_profile.api_gateway,
  ]
}
