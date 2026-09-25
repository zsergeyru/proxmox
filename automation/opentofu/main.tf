locals {
  vm_guests = {
    for vmid, guest in local.guests :
    vmid => guest
    if guest.type == "vm"
  }

  lxc_guests = {
    for vmid, guest in local.guests :
    vmid => guest
    if guest.type == "lxc"
  }
}

resource "proxmox_virtual_environment_vm" "guest" {
  for_each = local.vm_guests

  name        = each.value.name
  description = each.value.description
  node_name   = each.value.node
  vm_id       = each.value.vmid

  started    = true
  on_boot    = each.value.boot.onboot
  protection = each.value.protection

  # Обычное применение не должно автоматически останавливать гостя
  # ради параметра, который требует перезапуска.
  reboot_after_update = false

  clone {
    vm_id        = each.value.template_vmid
    datastore_id = each.value.resources.disk_storage
    full         = true
  }

  agent {
    enabled = true
  }

  cpu {
    cores = each.value.resources.cores
    type  = "host"
  }

  memory {
    dedicated = each.value.resources.memory_mb
    floating  = try(each.value.resources.ballooning_mb, 0)
  }

  scsi_hardware = "virtio-scsi-single"

  disk {
    datastore_id = each.value.resources.disk_storage
    interface    = "scsi0"
    size         = each.value.resources.disk_size_gb
    aio          = "io_uring"
    cache        = "none"
    discard      = "on"
    iothread     = true
    ssd          = true
  }

  initialization {
    datastore_id = each.value.resources.disk_storage
    interface    = "ide0"

    ip_config {
      ipv4 {
        address = each.value.network.ipv4
        gateway = each.value.network.ipv4 == "dhcp" ? null : each.value.network.gateway
      }
    }

    user_account {
      username = "root"
      keys     = [trimspace(var.ansible_ssh_public_key)]
    }
  }

  network_device {
    bridge = each.value.network.bridge
    model  = "virtio"
  }

  operating_system {
    type = "l26"
  }

  dynamic "startup" {
    for_each = (
      try(each.value.boot.order, null) != null ||
      try(each.value.boot.startup_delay_seconds, null) != null ||
      try(each.value.boot.shutdown_delay_seconds, null) != null
    ) ? [each.value.boot] : []

    content {
      order      = try(startup.value.order, null)
      up_delay   = try(startup.value.startup_delay_seconds, null)
      down_delay = try(startup.value.shutdown_delay_seconds, null)
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "proxmox_virtual_environment_container" "guest" {
  for_each = local.lxc_guests

  node_name   = each.value.node
  vm_id       = each.value.vmid
  description = each.value.description

  started       = true
  start_on_boot = each.value.boot.onboot
  protection    = each.value.protection
  unprivileged  = true

  cpu {
    cores = each.value.resources.cores
  }

  memory {
    dedicated = each.value.resources.memory_mb
    swap      = each.value.resources.swap_mb
  }

  disk {
    datastore_id = each.value.resources.disk_storage
    size         = each.value.resources.disk_size_gb
  }

  features {
    nesting = contains(each.value.features, "container-host")
    keyctl  = contains(each.value.features, "container-host")
  }

  initialization {
    hostname = each.value.name

    ip_config {
      ipv4 {
        address = each.value.network.ipv4
        gateway = each.value.network.ipv4 == "dhcp" ? null : each.value.network.gateway
      }
    }

    user_account {
      keys = [trimspace(var.ansible_ssh_public_key)]
    }
  }

  network_interface {
    name    = "eth0"
    bridge  = each.value.network.bridge
    enabled = true
  }

  operating_system {
    template_file_id = each.value.ostemplate_file_id
    type             = "debian"
  }

  dynamic "startup" {
    for_each = (
      try(each.value.boot.order, null) != null ||
      try(each.value.boot.startup_delay_seconds, null) != null ||
      try(each.value.boot.shutdown_delay_seconds, null) != null
    ) ? [each.value.boot] : []

    content {
      order      = try(startup.value.order, null)
      up_delay   = try(startup.value.startup_delay_seconds, null)
      down_delay = try(startup.value.shutdown_delay_seconds, null)
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
