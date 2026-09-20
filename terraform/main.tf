terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}

variable "key_path" {
  type        = string
  default     = ""
  description = "where the worker key is written, relative to this directory; defaults to ../secrets/google-secret-<project_id>.json, which is what a pipeline config's secret_files names"
}

locals {
  # Per project, never a bare google-secret.json: that directory already holds one key per
  # project, and a shared name would have this workspace overwrite another project's.
  key_path = coalesce(var.key_path, "") != "" ? var.key_path : "../secrets/google-secret-${var.project_id}.json"
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
