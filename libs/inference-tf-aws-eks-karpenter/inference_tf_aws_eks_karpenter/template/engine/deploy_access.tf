# === Push-path deploy role (opt-in): JscDeployRole -> namespaced Kubernetes RBAC ===
#
# The hosted service's sandbox executor assumes the customer's JscDeployRole and calls
# this cluster's API. EKS access policies hold fixed rules ("You can't modify the
# contents of an access policy … create Kubernetes RBAC"), and AmazonEKSEditPolicy covers
# core/apps kinds but no CRDs, so it cannot create KRO instances, InferencePools or
# PodMonitors. The role therefore joins the Kubernetes group `jsc-deploy`, and a
# namespaced Role/RoleBinding `jsc-deploy` in each allowed namespace grants:
#   - KRO instance kinds, pattern-based: every namespaced kind of var.deploy_kro_api_groups
#     (resources "*"), since each RGD version adds a kind. ResourceGraphDefinitions are
#     cluster-scoped, and a namespaced Role can never grant a cluster-scoped resource,
#     so admins keep sole control of RGDs;
#   - InferencePools, PodMonitors/ServiceMonitors and KEDA ScaledObjects.
# Optional (var.deploy_role_edit_policy): AmazonEKSEditPolicy scoped to the same
# namespaces, for core/apps kinds. Never cluster-admin, never cluster scope.
#
# One mechanism for both callers (push path and the UI-created cluster flow): set
# var.deploy_role_arn. Empty = nothing here is created.

locals {
  deploy_enabled = var.deploy_role_arn != ""
  deploy_group   = "jsc-deploy"
  deploy_verbs   = ["get", "list", "watch", "create", "update", "patch", "delete"]
  # for_each over namespaces only when enabled (plan-time known: both are variables).
  deploy_namespaces = local.deploy_enabled ? toset(var.deploy_namespaces) : toset([])
}

resource "aws_eks_access_entry" "deploy" {
  count = local.deploy_enabled ? 1 : 0

  cluster_name      = module.eks_cluster.cluster_name
  principal_arn     = var.deploy_role_arn
  kubernetes_groups = [local.deploy_group]
  type              = "STANDARD"
}

resource "aws_eks_access_policy_association" "deploy_edit" {
  count = local.deploy_enabled && var.deploy_role_edit_policy ? 1 : 0

  cluster_name  = module.eks_cluster.cluster_name
  principal_arn = var.deploy_role_arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"

  access_scope {
    type       = "namespace"
    namespaces = var.deploy_namespaces
  }

  depends_on = [aws_eks_access_entry.deploy]
}

resource "kubernetes_role_v1" "deploy" {
  for_each = local.deploy_namespaces

  metadata {
    name      = local.deploy_group
    namespace = each.value
    labels = {
      "app.kubernetes.io/managed-by" = "jupyter-deploy"
    }
  }

  # KRO instance kinds (pattern = whole API group, namespaced kinds only).
  rule {
    api_groups = var.deploy_kro_api_groups
    resources  = ["*"]
    verbs      = local.deploy_verbs
  }

  rule {
    api_groups = ["inference.networking.k8s.io"]
    resources  = ["inferencepools"]
    verbs      = local.deploy_verbs
  }

  rule {
    api_groups = ["monitoring.coreos.com"]
    resources  = ["podmonitors", "servicemonitors"]
    verbs      = local.deploy_verbs
  }

  rule {
    api_groups = ["keda.sh"]
    resources  = ["scaledobjects"]
    verbs      = local.deploy_verbs
  }

  # Read the status the executor reports back (pods, events) without the Edit policy.
  rule {
    api_groups = [""]
    resources  = ["pods", "pods/log", "events", "services"]
    verbs      = ["get", "list", "watch"]
  }

  depends_on = [
    null_resource.cluster_addons,
    kubernetes_namespace_v1.workload,
  ]
}

resource "kubernetes_role_binding_v1" "deploy" {
  for_each = local.deploy_namespaces

  metadata {
    name      = local.deploy_group
    namespace = each.value
    labels = {
      "app.kubernetes.io/managed-by" = "jupyter-deploy"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.deploy[each.key].metadata[0].name
  }

  subject {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Group"
    name      = local.deploy_group
  }

  depends_on = [
    null_resource.cluster_addons,
    kubernetes_namespace_v1.workload,
  ]
}
