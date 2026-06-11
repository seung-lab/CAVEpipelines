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

  # System logs only — by default GKE also ships every pod's stdout to Cloud Logging
  # (~$0.50/GiB past the free tier), pointless at thousands of chunk pods.
  # `kubectl logs` / `pipeline inspect` read from the kubelet and are unaffected.
  logging_config {
    enable_components = ["SYSTEM_COMPONENTS"]
  }

  # Metrics at the Autopilot-mandated minimum (system metrics can't be disabled, no
  # optional packages). Managed Prometheus is always-on for Autopilot but only bills
  # samples scraped via PodMonitoring resources — none are defined, so it idles free.
  monitoring_config {
    enable_components = ["SYSTEM_COMPONENTS"]
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
