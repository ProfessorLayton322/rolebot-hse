resource "yandex_lockbox_secret" "application" {
  name                = "${var.project_name}-runtime"
  description         = "Runtime credentials and dynamically refreshed admin configuration"
  folder_id           = var.yandex_folder_id
  deletion_protection = true
  labels              = local.labels
}

# Payload values are deliberately not Terraform resources. deploy.yml adds a new
# Lockbox version from GitHub Secrets after apply so plaintext never enters tfstate.

