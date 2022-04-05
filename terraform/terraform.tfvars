secret_file_path = "../cave-pipeline-fanc-fly.json"

common_name         = "cave-pipeline"
project_id          = "fanc-fly"
region              = "us-east1"
zone                = "us-east1-c"
preemptible_master  = false
preemptible_workers = true

worker_types        = {
  low = {
    count   = 0
    machine = "e2-standard-2"
    disk_size_gb = 15
  },
  # mid = {
  #   count   = 0
  #   machine = "e2-standard-4"
  #   disk_size_gb = 15
  # },
  # high = {
  #   count   = 0
  #   machine = "e2-standard-8"
  #   disk_size_gb = 15
  # },
}