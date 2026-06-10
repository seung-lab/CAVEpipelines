# GKE Autopilot: Google manages the nodes, so we declare no node pools. Pods
# request CPU/memory and select spot + machine class via nodeSelectors
# (cloud.google.com/gke-spot, cloud.google.com/compute-class). Workload Identity,
# autoscaling, and scale-to-zero between layers are on by default.
resource "google_container_cluster" "cluster" {
  name     = var.common_name
  location = var.region

  enable_autopilot = true

  # Ephemeral pipeline cluster — allow `terraform destroy` to tear it down.
  deletion_protection = false

  release_channel {
    channel = "REGULAR" # >= 1.33: backoffLimitPerIndex / podFailurePolicy:FailIndex are GA
  }

  resource_labels = {
    project = var.common_name
    owner   = var.owner
  }

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }
}
