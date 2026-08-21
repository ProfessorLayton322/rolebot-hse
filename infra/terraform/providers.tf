provider "yandex" {
  cloud_id  = var.yandex_cloud_id
  folder_id = var.yandex_folder_id
  zone      = var.yandex_zone
}

# Authentication is read from CLOUDFLARE_API_TOKEN; it is never a Terraform value.
provider "cloudflare" {}

