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

  guest_networks = {
    for vmid, guest in local.guests :
    vmid => try(
      guest.network.interfaces,
      [{
        name       = "eth0"
        bridge     = guest.network.bridge
        ipv4       = guest.network.ipv4
        gateway    = guest.network.ipv4 == "dhcp" ? null : guest.network.gateway
        management = true
      }]
    )
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

  startup {
    order    = try(tostring(each.value.boot.order), null)
    up_delay = try(each.value.boot.startup_delay_seconds, null)
  }

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

    dynamic "ip_config" {
      for_each = local.guest_networks[each.key]

      content {
        ipv4 {
          address = ip_config.value.ipv4
          gateway = ip_config.value.ipv4 == "dhcp" ? null : try(ip_config.value.gateway, null)
        }
      }
    }

    user_account {
      username = "root"
      keys     = [trimspace(var.ansible_ssh_public_key)]
    }
  }

  dynamic "network_device" {
    for_each = local.guest_networks[each.key]

    content {
      bridge  = network_device.value.bridge
      model   = "virtio"
      vlan_id = try(network_device.value.vlan, null)
    }
  }

  operating_system {
    type = "l26"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "proxmox_virtual_environment_container" "guest" {
  for_each = local.lxc_guests

  description  = each.value.description
  node_name    = each.value.node
  vm_id        = each.value.vmid
  unprivileged = true

  started       = true
  start_on_boot = each.value.boot.onboot
  protection    = each.value.protection

  startup {
    order    = try(tostring(each.value.boot.order), null)
    up_delay = try(each.value.boot.startup_delay_seconds, null)
  }

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
    # nesting управляется штатно через PVE API. keyctl является частью того же
    # container-host, но Proxmox разрешает менять его только root@pam, поэтому
    # общий deploy-guest применяет keyctl отдельным host-only шагом.
    nesting = contains(try(each.value.features, []), "container-host")
  }

  initialization {
    hostname = each.value.name

    dynamic "ip_config" {
      for_each = local.guest_networks[each.key]

      content {
        ipv4 {
          address = ip_config.value.ipv4
          gateway = ip_config.value.ipv4 == "dhcp" ? null : try(ip_config.value.gateway, null)
        }
      }
    }

    user_account {
      keys = [trimspace(var.ansible_ssh_public_key)]
    }
  }

  dynamic "network_interface" {
    for_each = local.guest_networks[each.key]

    content {
      name    = network_interface.value.name
      bridge  = network_interface.value.bridge
      vlan_id = try(network_interface.value.vlan, null)
    }
  }

  operating_system {
    template_file_id = each.value.ostemplate
    type             = "debian"
  }

  lifecycle {
    prevent_destroy = true

    # keyctl поддерживается общим host-only шагом deploy-guest и не должен
    # откатываться провайдером при следующем плане.
    ignore_changes = [features[0].keyctl]
  }
}
