resource "local_file" "helm_values" {
  filename = "${path.module}/../helm/config/${var.common_name}.yaml"
  content  = templatefile("${path.module}/helm_values.tpl", {
    redis_host       = google_redis_instance.redis.host
    google_project   = var.project_id
    bigtable_instance = google_bigtable_instance.instance.id
  })
}
