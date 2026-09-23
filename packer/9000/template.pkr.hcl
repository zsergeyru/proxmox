packer {
  required_version = ">= 1.15.0, < 1.16.0"

  required_plugins {
    proxmox = {
      version = "= 1.2.4"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

locals {
  template_vmid    = 9000
  template_name    = "tpl-debian13"
  template_version = 7
}

source "proxmox-iso" "template_9000" {
  proxmox_url = var.proxmox_url
  username    = var.proxmox_username
  token       = var.proxmox_token

  insecure_skip_tls_verify = false
  node                     = var.node
  task_timeout             = "20m"

  vm_id   = local.template_vmid
  vm_name = "builder-${local.template_vmid}"

  template_name        = local.template_name
  template_description = "Базовый Debian 13; template-version=${local.template_version}; managed-by=packer"
  tags                 = "template;debian13;packer"

  os                 = "l26"
  bios               = "seabios"
  machine            = "pc"
  cpu_type           = "host"
  sockets            = 1
  cores              = 1
  memory             = 1024
  ballooning_minimum = 0
  qemu_agent         = true
  onboot             = false

  scsi_controller = "virtio-scsi-single"
  serials         = ["socket"]

  vga {
    type = "std"
  }

  disks {
    type         = "scsi"
    disk_size    = "16G"
    storage_pool = var.disk_storage
    io_thread    = true
    discard      = true
    ssd          = true
  }

  network_adapters {
    model  = "virtio"
    bridge = var.bridge
  }

  boot = "order=scsi0;ide2;net0"

  boot_iso {
    type              = "ide"
    index             = "2"
    iso_url           = var.iso_url
    iso_checksum      = var.iso_checksum
    iso_storage_pool  = var.iso_storage
    iso_download_pve  = false
    unmount           = true
    keep_cdrom_device = false
  }

  cloud_init                          = true
  cloud_init_storage_pool             = var.disk_storage
  cloud_init_disk_type                = "ide"
  cloud_init_disable_upgrade_packages = true

  cd_content = {
    "preseed.cfg" = templatefile("${path.root}/http/preseed.cfg", {
      build_password = var.build_password
    })
  }
  cd_label = "PACKERPRESEED"

  boot_wait = "10s"
  boot_command = [
    "<esc><wait>",
    "install auto=true priority=critical ",
    "preseed/early_command=\"modprobe isofs; mkdir -p /tmp/packer-preseed; mount /dev/sr1 /tmp/packer-preseed; cp /tmp/packer-preseed/preseed.cfg /tmp/preseed.cfg; umount /tmp/packer-preseed\" ",
    "preseed/url=file:///tmp/preseed.cfg ",
    "locale=en_US.UTF-8 keyboard-configuration/xkb-keymap=us ",
    "interface=auto netcfg/get_hostname=builder-9000 netcfg/get_domain=local ",
    "<enter>"
  ]

  communicator = "ssh"
  ssh_username = "root"
  ssh_password = var.build_password
  ssh_timeout  = "30m"
}

build {
  sources = ["source.proxmox-iso.template_9000"]

  provisioner "shell" {
    scripts = [
      "${path.root}/scripts/setup.sh",
      "${path.root}/scripts/cleanup.sh",
    ]

    environment_vars = [
      "TEMPLATE_VERSION=${local.template_version}",
      "SOURCE_ISO=${var.iso_url}",
      "SOURCE_ISO_CHECKSUM=${var.iso_checksum}",
    ]
  }
}
