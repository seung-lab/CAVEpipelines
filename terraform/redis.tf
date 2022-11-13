variable "redis_memory_size_gb" {
  type        = number
  default     = 1
  description = "redis instance size"
}

resource "google_redis_instance" "redis" {
  name               = var.common_name
  display_name       = var.common_name
  tier               = "BASIC"
  memory_size_gb     = var.redis_memory_size_gb
  region             = var.region
  redis_version      = "REDIS_6_X"
  authorized_network = google_compute_network.vpc.name
  labels = {
    project = var.common_name
    owner   = var.owner
  }
}

