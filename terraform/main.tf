terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}


variable "common_name" {
  description = "common name to identify resources"
}

variable "owner" {
  type        = string
  description = "added as label to resources, convenient to filter costs based on labels"
  default     = "na"
}

variable "project_id" {
  description = "project id"
}

variable "region" {
  description = "region (Autopilot clusters are regional)"
}

variable "namespace" {
  type        = string
  default     = "default"
  description = "kubernetes namespace the pipeline pods run in"
}

variable "ksa_name" {
  type        = string
  default     = "pipeline"
  description = "kubernetes service account the pipeline pods use (bound to the worker GSA via Workload Identity)"
}

provider "google" {
  project = var.project_id
  region  = var.region
}
