import {
  to = yandex_storage_bucket.deployment
  id = var.function_package_bucket
}

# This pre-existing, versioned bucket contains both the Terraform backend state
# and the Cloud Functions deployment package. The lifecycle filter is confined
# to the package prefix so production.tfstate and all of its versions are never
# eligible for expiration.
resource "yandex_storage_bucket" "deployment" {
  bucket        = var.function_package_bucket
  folder_id     = var.yandex_folder_id
  max_size      = 50 * 1024 * 1024 * 1024
  force_destroy = false

  versioning {
    enabled = true
  }

  lifecycle_rule {
    id      = "expire-old-function-packages"
    enabled = true

    filter {
      prefix = "${var.project_name}/functions/"
    }

    # Keep a small rollback buffer and remove older packages after they have
    # spent at least one day as non-current versions.
    noncurrent_version_expiration {
      days                      = 1
      newer_noncurrent_versions = 3
    }
  }

  lifecycle_rule {
    id      = "expire-abandoned-disk-uploads"
    enabled = true

    filter {
      prefix = "${var.project_name}/uploads/"
    }

    expiration {
      days = 1
    }

    noncurrent_version_expiration {
      days = 1
    }
  }

  lifecycle_rule {
    id      = "expire-abandoned-disk-upload-locks"
    enabled = true

    filter {
      prefix = "${var.project_name}/upload-locks/"
    }

    expiration {
      days = 1
    }

    noncurrent_version_expiration {
      days = 1
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
