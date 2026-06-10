# One GSA shared by all three pipelines (ingest / l2cache / meshing). Pods
# impersonate it via Workload Identity (no mounted key needed on Autopilot).
resource "google_service_account" "worker" {
  account_id   = "${var.common_name}-worker"
  display_name = "CAVE pipeline worker (ingest / l2cache / meshing)"
}

locals {
  worker_roles = [
    "roles/bigtable.admin",      # create + read/write graph, cache, and meta tables
    "roles/storage.objectAdmin", # read edges/components/watershed; write meshes
  ]
}

resource "google_project_iam_member" "worker" {
  for_each = toset(local.worker_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.worker.email}"
}

# Workload Identity: let the in-cluster KSA act as this GSA.
resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}
