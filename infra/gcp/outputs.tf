output "controller_external_ip" {
  description = "Public IP of the controller VM. Hit http://<ip>:8080 from your home IP."
  value       = google_compute_instance.controller.network_interface[0].access_config[0].nat_ip
}

output "controller_internal_ip" {
  description = "Internal IP all workers use to reach the controller."
  value       = google_compute_address.controller_internal.address
}

output "vm_names" {
  description = "All VM names — useful for `gcloud compute instances stop` / start."
  value = concat(
    [google_compute_instance.controller.name],
    [for vm in google_compute_instance.workers : vm.name],
  )
}

output "zone" {
  description = "Zone all VMs live in. Used by vms.sh."
  value       = var.zone
}

output "ssh_commands" {
  description = "Copy-paste IAP SSH commands."
  value = merge(
    {
      (google_compute_instance.controller.name) = "gcloud compute ssh ${google_compute_instance.controller.name} --zone=${var.zone} --tunnel-through-iap"
    },
    {
      for name, vm in google_compute_instance.workers :
      name => "gcloud compute ssh ${name} --zone=${var.zone} --tunnel-through-iap"
    },
  )
}
