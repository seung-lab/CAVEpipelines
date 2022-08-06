resource "google_container_cluster" "cluster" {
  name                     = var.common_name
  location                 = var.zone
  remove_default_node_pool = true
  initial_node_count       = 1

  network                  = google_compute_network.vpc.self_link
  subnetwork               = google_compute_subnetwork.subnet.self_link
  networking_mode          = "VPC_NATIVE"

  release_channel {
    channel = "STABLE"
  }

  ip_allocation_policy {}

  addons_config {
    http_load_balancing {
      disabled = true
    }
  }

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }

  node_config {
    service_account = module.google_service_account.email
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
  }
}


variable "master_machine_type" {
  type        = string
  default     = "e2-small"
  description = "VM instance type for master pod"
}

variable "preemptible_master" {
  type        = bool
  default     = false
  description = "should master be preemptible?"
}

resource "google_container_node_pool" "master" {
  name       = "master"
  location   = var.zone
  cluster    = google_container_cluster.cluster.name
  node_count = 1

  node_config {
    labels = {
      project = var.common_name
    }

    preemptible  = var.preemptible_master
    machine_type = var.master_machine_type
    disk_size_gb = 15

    tags         = ["${var.common_name}-master"]
    metadata = {
      disable-legacy-endpoints = "true"
    }
    service_account = module.google_service_account.email
    oauth_scopes    = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
  }
}
