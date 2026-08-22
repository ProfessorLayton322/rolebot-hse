# Transitional declaration used to adopt the pre-existing, unused KMS key into
# Terraform state. A follow-up change removes this resource so destruction is
# performed exclusively by the GitHub Actions Terraform deployment workflow.
import {
  to = yandex_kms_symmetric_key.unused
  id = "abj488nqe9mri92un3vd"
}

resource "yandex_kms_symmetric_key" "unused" {
  folder_id           = var.yandex_folder_id
  name                = "key-1787276166800"
  description         = "kms key"
  default_algorithm   = "AES_256"
  deletion_protection = false
}
