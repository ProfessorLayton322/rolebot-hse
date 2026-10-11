mock_provider "aws" {
  override_during = plan

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = jsonencode({
        Version   = "2012-10-17"
        Statement = []
      })
    }
  }

  mock_resource "aws_cloudwatch_metric_alarm" {
    defaults = {
      arn = "arn:aws:cloudwatch:eu-central-1:319215798726:alarm:mock"
    }
  }

  mock_resource "aws_lambda_event_source_mapping" {
    defaults = {
      uuid = "00000000-0000-0000-0000-000000000000"
    }
  }
}

variables {
  cloudflare_workers_subdomain   = "example"
  google_folder_id               = "example"
  tg_admin_ids                   = "[]"
  vk_admin_ids                   = "[]"
  tg_gamemaster_ids              = "[]"
  vk_gamemaster_ids              = "[]"
  python_application_zip_path    = "../../../dist/function-package.zip"
  registration_notifier_zip_path = "../../../dist/registration-notifier.zip"
  disk_uploader_zip_path         = "../../../dist/disk-uploader.zip"
}

run "dashboard_metrics_are_row_arrays" {
  command = plan

  assert {
    condition = alltrue([
      for widget in jsondecode(aws_cloudwatch_dashboard.operations.dashboard_body).widgets :
      alltrue([for row in try(widget.properties.metrics, []) : can(row[0])])
    ])
    error_message = "Every CloudWatch dashboard metric entry must be an array row."
  }
}
