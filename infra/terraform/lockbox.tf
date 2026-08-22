resource "yandex_lockbox_secret" "application" {
  name        = "${var.project_name}-runtime"
  description = "Runtime credentials and dynamically refreshed admin configuration"
  folder_id   = var.yandex_folder_id
  # Transitional: protection is disabled before this now-unused paid resource
  # is removed in the follow-up Terraform deployment.
  deletion_protection = false
  labels              = local.labels
}

# No new payload versions are written. The application has already migrated to
# encrypted Cloudflare Worker bindings before this container is deleted.
