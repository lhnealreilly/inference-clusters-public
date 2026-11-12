# === Load balancers: endpoint + destroy-time cleanup ===
#
# Lesson from the gemma teardown: on the endpoints-only VPC the in-cluster AWS Load
# Balancer Controller (bundled with the HyperPod inference add-on) hung deleting the
# gateway Ingress, most likely because there is no `tagging` (Resource Groups Tagging
# API) interface endpoint: the LBC resolves its load balancers through
# tagging:GetResources. The LB then outlives the controller and VPC deletion fails.
#
# - Endpoint: `tagging`, whenever something in the cluster can own a load balancer
#   (Auto Mode's load balancing, the plain inference gateway, the HyperPod gateway's LBC).
#   Auto Mode's own load-balancing controller runs in the EKS control plane, not in the
#   VPC, so it needs no endpoint of its own; elasticloadbalancing and ec2 are already in
#   the base set and the HyperPod path adds acm.
# - Cleanup: before the gateway/add-on releases (and so before EKS) are destroyed,
#   delete every object that owns an AWS load balancer (InferenceGatewayConfigs,
#   Gateways, Ingresses, LoadBalancer Services), then wait until no load balancer is
#   left in this VPC. If one is still there after the timeout, FAIL the destroy with the
#   ARNs rather than leave an orphaned LB that blocks VPC deletion.

locals {
  lb_capable = var.enable_auto_mode || local.inference_gateway_enabled || var.enable_hyperpod_inference_gateway
}

resource "aws_vpc_endpoint" "tagging" {
  count = local.lb_capable ? 1 : 0

  vpc_id              = module.vpc.vpc_id
  service_name        = "com.amazonaws.${data.aws_region.current.id}.tagging"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = module.vpc.private_subnet_ids
  security_group_ids  = [module.vpc.endpoint_security_group_id]
  private_dns_enabled = true

  tags = merge(local.combined_tags, {
    Name = "${local.resource_name_prefix}-vpce-tagging"
  })
}

# Destroy order: this resource depends on the LB-owning controllers, so it is destroyed
# (its provisioner runs) BEFORE them, while they can still delete their load balancers.
# The triggers reference the cluster endpoint and the admin access entries as attributes
# so the API and the caller's authorization outlive the cleanup.
resource "null_resource" "lb_cleanup" {
  count = local.lb_capable ? 1 : 0

  triggers = {
    cluster_name       = module.eks_cluster.cluster_name
    region             = var.region
    vpc_id             = module.vpc.vpc_id
    cluster_endpoint   = module.eks_cluster.cluster_endpoint
    admin_access_entry = join(",", [for e in aws_eks_access_entry.admin_role : e.id])
    tagging_endpoint   = aws_vpc_endpoint.tagging[0].id

    script = <<-EOT
      set -uo pipefail
      CLUSTER_NAME="${module.eks_cluster.cluster_name}"
      REGION="${var.region}"
      VPC_ID="${module.vpc.vpc_id}"
      MAX_WAIT_SECONDS=600
      SLEEP_INTERVAL=15

      export KUBECONFIG
      KUBECONFIG="$(mktemp)"
      if aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$REGION" --kubeconfig "$KUBECONFIG" >/dev/null 2>&1; then
        echo "[lb-cleanup] deleting load-balancer owners in $CLUSTER_NAME..."
        # Owners first (their controllers delete the Ingress/Service they generated).
        for kind in inferencegatewayconfigs.inference.sagemaker.aws.amazon.com gateways.gateway.networking.k8s.io; do
          if kubectl get crd "$kind" >/dev/null 2>&1; then
            kubectl delete "$kind" --all -A --ignore-not-found --wait=false 2>&1 || true
          fi
        done
        sleep "$SLEEP_INTERVAL"
        kubectl delete ingress --all -A --ignore-not-found --wait=false 2>&1 || true
        kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}{" "}{.metadata.name}{"\n"}{end}' 2>/dev/null \
          | while read -r ns name; do
              [ -n "$ns" ] && kubectl delete svc -n "$ns" "$name" --ignore-not-found --wait=false 2>&1 || true
            done
      else
        echo "[lb-cleanup] WARNING: could not reach the cluster API; only checking for leftover load balancers."
      fi

      waited=0
      while true; do
        lbs="$(aws elbv2 describe-load-balancers --region "$REGION" \
          --query "LoadBalancers[?VpcId=='$VPC_ID'].LoadBalancerArn" --output text 2>/dev/null | tr '\t' ' ')"
        if [ -z "$lbs" ] || [ "$lbs" = "None" ]; then
          echo "[lb-cleanup] no load balancers left in $VPC_ID."
          break
        fi
        if [ "$waited" -ge "$MAX_WAIT_SECONDS" ]; then
          echo "[lb-cleanup] ERROR: load balancers still in $VPC_ID after $${MAX_WAIT_SECONDS}s: $lbs" >&2
          echo "[lb-cleanup] The in-cluster controller did not delete them (check its logs; on a no-NAT VPC a missing interface endpoint hangs it)." >&2
          echo "[lb-cleanup] Delete them (aws elbv2 delete-load-balancer --load-balancer-arn <arn>) and their target groups, then re-run jd down." >&2
          rm -f "$KUBECONFIG"
          exit 1
        fi
        echo "[lb-cleanup]   waiting for: $lbs ($${waited}s / $${MAX_WAIT_SECONDS}s)"
        sleep "$SLEEP_INTERVAL"
        waited=$((waited + SLEEP_INTERVAL))
      done
      rm -f "$KUBECONFIG"
    EOT
  }

  # The script lives in state so destroy works after a template upgrade. A later script
  # edit must NOT replace this resource on a live cluster (replacement would run the
  # destroy provisioner and delete the gateway's load balancers mid-apply), so changes to
  # triggers are ignored; the depends_on/attribute edges still come from the config.
  lifecycle {
    ignore_changes = [triggers]
  }

  provisioner "local-exec" {
    when        = destroy
    interpreter = ["/bin/bash", "-c"]
    command     = self.triggers.script
  }

  depends_on = [
    aws_eks_addon.hyperpod_inference,
    helm_release.hyperpod_gateway,
    helm_release.inference_gateway,
    helm_release.ai_gateway,
    helm_release.envoy_gateway,
    null_resource.cluster_addons,
    module.node_group,
  ]
}
