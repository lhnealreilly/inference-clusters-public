resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.cluster_name}/cluster"
  retention_in_days = var.cluster_log_retention_days
  tags              = var.combined_tags
}

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  version  = var.kubernetes_version
  role_arn = var.cluster_role_arn

  vpc_config {
    subnet_ids              = concat(var.private_subnet_ids, var.public_subnet_ids)
    endpoint_private_access = true
    endpoint_public_access  = true
    public_access_cidrs     = var.public_access_cidrs
  }

  # EKS Auto Mode (opt-in). The three configs must be enabled together. No built-in
  # pools: they are unbounded, unlabeled and cannot be limited; the template ships its
  # own NodeClasses/NodePools (charts/karpenter). node_role_arn is omitted: "Create an
  # EKS cluster with both nodePools and nodeRoleArn values empty".
  # bootstrap_self_managed_addons stays at its default (true): the platform node group
  # still needs vpc-cni / kube-proxy / coredns.
  dynamic "compute_config" {
    for_each = var.enable_auto_mode ? [1] : []
    content {
      enabled    = true
      node_pools = []
    }
  }

  dynamic "storage_config" {
    for_each = var.enable_auto_mode ? [1] : []
    content {
      block_storage {
        enabled = true
      }
    }
  }

  dynamic "kubernetes_network_config" {
    for_each = var.enable_auto_mode ? [1] : []
    content {
      elastic_load_balancing {
        enabled = true
      }
    }
  }

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = false
  }

  enabled_cluster_log_types = [
    "api",
    "audit",
    "authenticator",
    "controllerManager",
    "scheduler",
  ]

  tags = var.combined_tags

  depends_on = [aws_cloudwatch_log_group.cluster]
}
