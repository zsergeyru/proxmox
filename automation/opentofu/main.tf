locals {
  guest_410 = local.guests["410"]
}

resource "proxmox_virtual_environment_vm" "guest_410" {
  name        = local.guest_410.name
  description = local.guest_410.description
  node_name   = local.guest_410.node
  vm_id       = local.guest_410.vmid

  started    = true
  on_boot    = local.guest_410.boot.onboot
  protection = local.guest_410.protection

  # Обычное применение не должно автоматически останавливать 410
  # ради параметра, который требует перезапуска.
  reboot_after_update = false

  clone {
    vm_id        = local.guest_410.template_vmid
    datastore_id = local.guest_410.resources.disk_storage
    full         = true
  }

  agent {
    enabled = true
  }

  cpu {
    cores = local.guest_410.resources.cores
    type  = "host"
  }

  memory {
    dedicated = local.guest_410.resources.memory_mb
    floating  = try(local.guest_410.resources.ballooning_mb, 0)
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = local.guest_410.resources.disk_storage
    interface    = "scsi0"
    size         = local.guest_410.resources.disk_size_gb
    aio          = "io_uring"
    cache        = "none"
    discard      = "on"
    iothread     = true
    ssd          = true
  }

  initialization {
    datastore_id = local.guest_410.resources.disk_storage

    ip_config {
      ipv4 {
        address = local.guest_410.network.ipv4
        gateway = local.guest_410.network.ipv4 == "dhcp" ? null : local.guest_410.network.gateway
      }
    }

    user_account {
      username = "root"
      keys     = [trimspace(var.ansible_ssh_public_key)]
    }
  }

  network_device {
    bridge = local.guest_410.network.bridge
    model  = "virtio"
  }

  operating_system {
    type = "l26"
  }

  lifecycle {
    prevent_destroy = true
  }
}
