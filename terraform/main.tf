terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "4.15.0"
    }
  }
}

variable "secret_file_path" {
  description = "path to service account secret for creating infrastructure"
}


variable "common_name" {
  description = "common name to identify resources"
}

variable "project_id" {
  description = "project id"
}

variable "region" {
  description = "region"
}

variable "zone" {
  description = "zone"
}

provider "google" {
  credentials = file(var.secret_file_path)
  project = var.project_id
  region  = var.region
}
