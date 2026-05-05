# Controller — gets the reserved internal IP and an ephemeral external IP
# so you can hit the dashboard from your laptop.
resource "google_compute_instance" "controller" {
  name         = "controller-01"
  machine_type = var.controller_machine_type
  zone         = var.zone

  tags = ["controller"]

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = var.boot_disk_size_gb
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
    network_ip = google_compute_address.controller_internal.address

    # Ephemeral external IP. To pin it (paid if unattached), swap for an
    # access_config block referencing a google_compute_address resource.
    access_config {}
  }

  metadata = {
    device-id      = "controller-01"
    device-type    = "controller"
    device-cluster = var.cluster_name
    controller-url = "http://${google_compute_address.controller_internal.address}:8080"
    startup-script = file("${path.module}/startup.sh")
  }
}

# Workers — 2 servers + 2 edges. Internal IPs only; reach via IAP SSH or
# from the controller VM.
locals {
  workers = {
    "server-01" = { device_type = "server", machine_type = var.server_machine_type }
    "server-02" = { device_type = "server", machine_type = var.server_machine_type }
    "edge-01"   = { device_type = "edge",   machine_type = var.edge_machine_type }
    "edge-02"   = { device_type = "edge",   machine_type = var.edge_machine_type }
  }
}

resource "google_compute_instance" "workers" {
  for_each     = local.workers
  name         = each.key
  machine_type = each.value.machine_type
  zone         = var.zone

  tags = [each.value.device_type]

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = var.boot_disk_size_gb
    }
  }

  # Internal-only — no access_config block.
  network_interface {
    subnetwork = google_compute_subnetwork.subnet.id
  }

  metadata = {
    device-id      = each.key
    device-type    = each.value.device_type
    device-cluster = var.cluster_name
    controller-url = "http://${google_compute_address.controller_internal.address}:8080"
    startup-script = file("${path.module}/startup.sh")
  }
}
