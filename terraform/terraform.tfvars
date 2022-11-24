common_name          = "cave-pipeline"
project_id           = "<>"
region               = "us-east1"
zone                 = "us-east1-c"
master_machine_type  = "e2-small"
preemptible_master   = false
preemptible_workers  = true
redis_memory_size_gb = 1

worker_types = {
  low = {
    count   = 1
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