variable "preemptible_workers" {
  type        = bool
  default     = true
  description = "should workers be preemptible?"
}

variable "worker_types" {
  type = map(object({
    count        = number
    machine      = string
    disk_size_gb = number
  }))
  default = {
    low = {
      count   = 16
      machine = "e2-standard-2"
      disk_size_gb = 15
    },
    mid = {
      count   = 8
      machine = "e2-standard-4"
      disk_size_gb = 15
    },
    high = {
      count   = 4
      machine = "e2-standard-8"
      disk_size_gb = 15
    },
  }
}



resource "google_container_node_pool" "pool" {
  for_each = var.worker_types

  name       = each.key
  location   = var.zone
  cluster    = google_container_cluster.cluster.name
  node_count = each.value.count

  node_config {
    labels = {
      project = var.common_name
    }
    preemptible  = var.preemptible_workers
    machine_type = each.value.machine
    disk_size_gb = each.value.disk_size_gb

    tags         = ["${var.common_name}-${each.key}"]
    metadata     = {
      disable-legacy-endpoints = "true"
    }
    service_account = module.google_service_account.email
    oauth_scopes    = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
  }
}
