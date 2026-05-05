provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

# Enable Compute Engine API (idempotent; no-op after first apply).
resource "google_project_service" "compute" {
  service            = "compute.googleapis.com"
  disable_on_destroy = false
}

# --- Network ---
resource "google_compute_network" "vpc" {
  name                    = "streambed-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.compute]
}

resource "google_compute_subnetwork" "subnet" {
  name          = "streambed-subnet"
  region        = var.region
  network       = google_compute_network.vpc.id
  ip_cidr_range = "10.10.0.0/24"
}

# --- Firewall rules ---

# All TCP/UDP/ICMP between VMs in the subnet.
resource "google_compute_firewall" "allow_internal" {
  name    = "streambed-allow-internal"
  network = google_compute_network.vpc.name

  source_ranges = [google_compute_subnetwork.subnet.ip_cidr_range]

  allow { protocol = "tcp" }
  allow { protocol = "udp" }
  allow { protocol = "icmp" }
}

# IAP tunnel CIDR — lets `gcloud compute ssh --tunnel-through-iap` work
# without exposing port 22 to the public internet.
resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "streambed-allow-iap-ssh"
  network = google_compute_network.vpc.name

  source_ranges = ["35.235.240.0/20"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Dashboard / API access from your home IP only. Tighten this; do not 0.0.0.0/0.
resource "google_compute_firewall" "allow_controller_http" {
  name    = "streambed-allow-controller-http"
  network = google_compute_network.vpc.name

  source_ranges = [var.your_home_ip_cidr]
  target_tags   = ["controller"]

  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }
}

# Reserved internal IP for the controller. Workers use this in their
# CONTROLLER_URL metadata so they can find it deterministically.
resource "google_compute_address" "controller_internal" {
  name         = "streambed-controller-internal"
  subnetwork   = google_compute_subnetwork.subnet.id
  address_type = "INTERNAL"
  region       = var.region
}
