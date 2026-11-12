variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type = string
}

variable "cluster_role_arn" {
  type = string
}

variable "cluster_log_retention_days" {
  type = number
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "public_access_cidrs" {
  type        = list(string)
  description = <<-EOT
    CIDRs allowed to reach the public control-plane endpoint. The endpoint is a
    knock-surface, not an auth boundary — EKS access entries (IAM) are the real
    gate. Open by default; tighten to team CIDRs beyond the POC.
  EOT
}

variable "enable_auto_mode" {
  type        = bool
  description = "Enable EKS Auto Mode compute, block storage and load balancing (no built-in node pools)."
}

variable "combined_tags" {
  type = map(string)
}
