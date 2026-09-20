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
    "roles/container.developer", # the driver reads and submits Jobs as this GSA
  ]
}

resource "google_project_iam_member" "worker" {
  for_each = toset(local.worker_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.worker.email}"
}

# The driver runs outside the cluster, so Workload Identity does not reach it. A user credential
# would: it is subject to reauth, which needs a TTY and a person, and a run outlives the session
# length. This key has neither property and is the same identity the pods already use.
#
# The private key is held in terraform state in plaintext — the provider says so in its own docs —
# so the state is as sensitive as the key, and `terraform destroy` is what revokes both.
resource "google_service_account_key" "worker" {
  service_account_id = google_service_account.worker.name
}

resource "local_sensitive_file" "worker_key" {
  filename        = local.key_path
  content         = base64decode(google_service_account_key.worker.private_key)
  file_permission = "0600"
}

# Workload Identity: let the in-cluster KSA act as this GSA.
resource "google_service_account_iam_member" "workload_identity" {
  service_account_id = google_service_account.worker.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "serviceAccount:${var.project_id}.svc.id.goog[${var.namespace}/${var.ksa_name}]"
}
