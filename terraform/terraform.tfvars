secret_file_path = "../cave-pipeline-fanc-fly.json"

common_name         = "cave-pipeline"
project_id          = "fanc-fly"
region              = "us-east1"
zone                = "us-east1-c"
preemptible_master  = false
preemptible_workers = true

worker_types        = {
  low = {
    count   = 100
    machine = "e2-standard-2"
  },
  # mid = {
  #   count   = 0
  #   machine = "e2-standard-4"
  # },
  # high = {
  #   count   = 0
  #   machine = "e2-standard-8"
  # },
}