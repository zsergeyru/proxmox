variable "pve_endpoint" {
  description = "HTTPS endpoint Proxmox API"
  type        = string
}

variable "pve_api_token" {
  description = "API token в формате user@realm!token=secret"
  type        = string
  sensitive   = true
}

variable "guest_state_file" {
  description = "Путь к JSON с итоговым состоянием гостей"
  type        = string
  default     = "/var/lib/infra-manager/opentofu/guests.json"
}

locals {
  guest_state = jsondecode(file(var.guest_state_file))
  guests      = local.guest_state.guests
}


variable "ansible_ssh_public_key" {
  description = "Открытый SSH-ключ Ansible для Cloud-Init управляемых Linux-гостей"
  type        = string

  validation {
    condition     = length(trimspace(var.ansible_ssh_public_key)) > 0
    error_message = "ansible_ssh_public_key не должен быть пустым"
  }
}
