terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "4.15.0"
    }
  }
}


variable "common_name" {
  description = "common name to identify resources"
}

variable "owner" {
  type = string
  description = "added as label to resources, convenient to filter costs based on labels"
  default = "na"
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
  project = var.project_id
  region  = var.region
}
