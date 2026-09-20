output "region" {
  value       = var.region
  description = "GCloud Region"
}

output "project_id" {
  value       = var.project_id
  description = "GCloud Project ID"
}

output "worker_service_account" {
  value       = google_service_account.worker.email
  description = "GSA the pipeline pods impersonate via Workload Identity (annotate the KSA with this)"
}

output "worker_key_path" {
  value       = local_sensitive_file.worker_key.filename
  description = "worker key on disk; the driver authenticates with it as GOOGLE_APPLICATION_CREDENTIALS"
}

output "kubernetes_cluster_name" {
  value       = google_container_cluster.cluster.name
  description = "GKE Autopilot cluster name"
}

output "kubernetes_cluster_context" {
  value       = "gcloud container clusters get-credentials ${google_container_cluster.cluster.name} --region ${var.region} --project ${var.project_id}"
  description = "Command to fetch cluster credentials"
}
