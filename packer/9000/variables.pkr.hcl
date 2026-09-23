variable "proxmox_url" {
  type        = string
  description = "HTTPS API Proxmox, включая /api2/json"
}

variable "proxmox_username" {
  type        = string
  description = "API token id в формате user@realm!token"
}

variable "proxmox_token" {
  type        = string
  description = "Secret API token"
  sensitive   = true
}

variable "node" {
  type        = string
  description = "Узел Proxmox для сборки"
  default     = "pve"
}

variable "disk_storage" {
  type        = string
  description = "Хранилище системного диска и Cloud-Init"
  default     = "local-lvm"
}

variable "iso_storage" {
  type        = string
  description = "Хранилище Proxmox с типом content=iso"
  default     = "local"
}

variable "bridge" {
  type        = string
  description = "Сетевой мост VM-сборщика"
  default     = "vmbr0"
}

variable "iso_url" {
  type        = string
  description = "URL Debian 13 amd64 netinst ISO"

  validation {
    condition     = can(regex("^https://", var.iso_url))
    error_message = "Значение iso_url должно использовать HTTPS."
  }
}

variable "iso_checksum" {
  type        = string
  description = "Контрольная сумма ISO с алгоритмом, например sha512:<hash>"

  validation {
    condition     = can(regex("^(sha256|sha512):[0-9a-fA-F]+$", var.iso_checksum))
    error_message = "Значение iso_checksum должно содержать sha256: или sha512: и контрольную сумму."
  }
}

variable "build_password" {
  type        = string
  description = "Одноразовый пароль root только на время сборки"
  sensitive   = true

  validation {
    condition     = length(var.build_password) >= 20 && can(regex("^[A-Za-z0-9]+$", var.build_password))
    error_message = "Значение build_password должно быть не короче 20 символов и состоять только из букв и цифр."
  }
}
