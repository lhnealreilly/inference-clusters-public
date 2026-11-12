# === GPU serving path ===
#
# The NVIDIA device plugin advertises GPUs as schedulable resources.
# Its image (and DCGM's) lives on nvcr.io with no no-creds mirror,
# so both are vendored into our ECR via CodeBuild (images_vendored.tf).
# Nodes pull the vendored copies from private ECR like any other image.

# GPU AMI cannot be selected by a Karpenter alias (no al2023-nvidia alias family);
# resolve the EKS-optimized AL2023 NVIDIA AMI ID from SSM and inject it into the
# gpu EC2NodeClass by id.
data "aws_ssm_parameter" "gpu_ami" {
  name = "/aws/service/eks/optimized-ami/${var.kubernetes_version}/amazon-linux-2023/x86_64/nvidia/recommended/image_id"
}

# --- NVIDIA device plugin (DaemonSet, tolerate-all so it runs on GPU nodes) ---
#
# Not on Auto Mode: Auto Mode bundles the NVIDIA driver + device plugin ("You do not have
# to install or update these components"), and a second plugin on the same node would
# fight over nvidia.com/gpu.
resource "helm_release" "nvidia_device_plugin" {
  count = var.enable_auto_mode ? 0 : 1

  name       = "nvidia-device-plugin"
  repository = "https://nvidia.github.io/k8s-device-plugin"
  chart      = "nvidia-device-plugin"
  version    = var.nvidia_device_plugin_chart_version
  namespace  = "kube-system"

  set = [
    {
      name  = "image.repository"
      value = aws_ecr_repository.vendored["device_plugin"].repository_url
    },
    { name = "image.tag", value = local.vendored_tag },
    # DaemonSet must tolerate all taints so it lands on the GPU NodePool nodes
    # (which carry nvidia.com/gpu=present:NoSchedule).
    { name = "tolerations[0].operator", value = "Exists" },
  ]

  depends_on = [
    null_resource.cluster_addons,
    null_resource.image_vendor,
    helm_release.karpenter,
  ]
}

moved {
  from = helm_release.nvidia_device_plugin
  to   = helm_release.nvidia_device_plugin[0]
}
