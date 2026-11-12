variable "role_name" {
  type = string
}

variable "assume_role_policy" {
  type = string
}

variable "policy_arns" {
  type    = list(string)
  default = []
}

variable "combined_tags" {
  type = map(string)
}

variable "permissions_boundary" {
  type        = string
  default     = null
  description = "IAM permissions boundary ARN set on the role (null = none)."
}
