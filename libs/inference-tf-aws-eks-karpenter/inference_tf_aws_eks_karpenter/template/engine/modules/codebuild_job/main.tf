# Reusable CodeBuild job for image mirroring/vendoring (nvcr.io → our ECR today,
# onboarder workload vendoring later). Modeled on the eks-oidc
# codebuild_job module: trust → role → permissions → project. The job is
# NO_SOURCE and env-driven; callers pass per-run source/dest via start-build
# environment-variable overrides.

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name                 = "${var.project_name}-codebuild"
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  permissions_boundary = var.permissions_boundary
  tags                 = var.combined_tags
}

data "aws_iam_policy_document" "permissions" {
  statement {
    sid       = "ECRAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # Push (and read-back) only to the specific repos this job targets.
  statement {
    sid = "ECRPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = var.ecr_repository_arns
  }

  statement {
    sid       = "CloudWatchLogs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "this" {
  name   = "${var.project_name}-permissions"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.permissions.json
}

# Optional extra grant (e.g. the onboarder S3 weights ingest read/write). Gated on the
# plan-time-known attach_extra_policy flag, NOT on the JSON content: the onboarder's policy
# references the model-store bucket ARN (bucket_prefix => unknown until apply), so
# `extra_policy_json == ""` would be an unknown count and fail `terraform plan`.
resource "aws_iam_role_policy" "extra" {
  count  = var.attach_extra_policy ? 1 : 0
  name   = "${var.project_name}-extra"
  role   = aws_iam_role.this.id
  policy = var.extra_policy_json
}

# AWS-managed policy attachments (e.g. AmazonS3ReadOnlyAccess for reading weight sources).
resource "aws_iam_role_policy_attachment" "managed" {
  for_each   = toset(var.managed_policy_arns)
  role       = aws_iam_role.this.name
  policy_arn = each.value
}

resource "aws_codebuild_project" "this" {
  name         = var.project_name
  service_role = aws_iam_role.this.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  source {
    type      = "NO_SOURCE"
    buildspec = var.buildspec
  }

  environment {
    compute_type    = var.compute_type
    image           = "aws/codebuild/standard:7.0"
    type            = "LINUX_CONTAINER"
    privileged_mode = true

    dynamic "environment_variable" {
      for_each = var.environment_variables
      content {
        name  = environment_variable.key
        value = environment_variable.value
      }
    }
  }

  tags = var.combined_tags
}
