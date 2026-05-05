variable "project_id" {
  description = "Your GCP project ID (create one in the console first, attach billing/credits)."
  type        = string
}

variable "region" {
  description = "GCP region. us-central1 is the cheapest and free-tier eligible."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone within the region."
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "Logical cluster name baked into VM metadata; used as device_cluster in StreamBed."
  type        = string
  default     = "gcp-test"
}

variable "your_home_ip_cidr" {
  description = "Your home/office public IP (e.g. 1.2.3.4/32) for SSH-IAP fallback and dashboard access. Find it via `curl ifconfig.me`."
  type        = string
}

# Machine sizing — see docs/GCPTestInfra.md §"Machine sizing" for trade-offs.
variable "controller_machine_type" {
  description = "VM size for the controller. e2-micro is fine."
  type        = string
  default     = "e2-micro"
}

variable "edge_machine_type" {
  description = "VM size for edges. e2-micro is fine."
  type        = string
  default     = "e2-micro"
}

variable "server_machine_type" {
  description = "VM size for inference servers. e2-small recommended for PyTorch headroom; try e2-micro to save money."
  type        = string
  default     = "e2-small"
}

variable "boot_disk_size_gb" {
  description = "Boot disk size per VM."
  type        = number
  default     = 10
}

variable "boot_image" {
  description = "Boot image. Ubuntu 22.04 is the closest familiar baseline if you're coming from AWS."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2204-lts"
}
