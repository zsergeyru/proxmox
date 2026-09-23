terraform {
  required_version = ">= 1.12.0, < 1.13.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.112.0"
    }
  }

  backend "local" {
    path = "/var/lib/infra-manager/opentofu/state/proxmox.tfstate"
  }
}
