module "google_service_account" {
  source        = "terraform-google-modules/service-accounts/google"
  version       = "~> 4.1.1"
  project_id    = "${var.project_id}"
  prefix        = "${var.common_name}"
  names         = ["worker-svc"]
  project_roles = [
    "${var.project_id}=>roles/bigtable.admin",                   # Generate table within Bigtable instance
    "${var.project_id}=>roles/storage.objectAdmin",              # Generate/overwrite components/edge bucket objects
    "${var.project_id}=>roles/containerregistry.ServiceAgent",   # Access PCG images on GCR
  ]
}