variable "project_name" {
  type = string
}

variable "buildspec" {
  type        = string
  description = "Inline buildspec (YAML) the project runs. Source is NO_SOURCE — the job is env-driven."
}

variable "ecr_repository_arns" {
  type        = list(string)
  description = "ECR repository ARNs the job is allowed to push to (mirror/vendor targets)."
}

variable "environment_variables" {
  type        = map(string)
  description = "Static environment variables set on the project. Per-run values are passed via start-build overrides."
}

variable "combined_tags" {
  type = map(string)
}

variable "compute_type" {
  type        = string
  description = "CodeBuild compute type. Default SMALL fits image mirroring; onboarder weight ingest (10s-100s of GB) needs a larger type."
  default     = "BUILD_GENERAL1_SMALL"
}

variable "extra_policy_json" {
  type        = string
  description = "Optional additional IAM policy (JSON) attached to the job role — e.g. S3 read/write for the onboarder weights ingest. Empty = none."
  default     = ""
}

variable "attach_extra_policy" {
  type        = bool
  description = "Whether to attach extra_policy_json. Must be a plan-time-known flag: gating the policy resource on `extra_policy_json != \"\"` fails when the JSON references an apply-time-unknown value (e.g. a bucket_prefix ARN), so the caller sets this explicitly."
  default     = false
}

variable "managed_policy_arns" {
  type        = list(string)
  description = "AWS-managed policy ARNs to attach to the job role — e.g. AmazonS3ReadOnlyAccess so the onboarder can read any weight-source bucket. Empty = none."
  default     = []
}

variable "permissions_boundary" {
  type        = string
  default     = null
  description = "IAM permissions boundary ARN set on the role (null = none)."
}
