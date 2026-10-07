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


variable "bootstrap_ssh_public_key" {
  description = "Одноразовый открытый SSH-ключ первоначального доступа к новому Linux-гостю"
  type        = string

  validation {
    condition     = length(trimspace(var.bootstrap_ssh_public_key)) > 0
    error_message = "bootstrap_ssh_public_key не должен быть пустым"
  }
}
