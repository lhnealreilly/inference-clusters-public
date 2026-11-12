# === Plain inference gateway (opt-in; no SageMaker HyperPod add-on) ===
#
# The tier-1 routing telemetry (EPP + BBR metrics) without HyperPod:
#   - Envoy Gateway (oci://docker.io/envoyproxy/gateway-helm) as the Gateway API
#     implementation, in GatewayNamespace mode so the Envoy data plane runs in the
#     workload namespace next to the InferencePools.
#   - Envoy AI Gateway (oci://docker.io/envoyproxy/ai-gateway-helm): Envoy Gateway's
#     extension server. It is what turns an HTTPRoute backendRef of kind InferencePool
#     into an EPP-routed cluster (Envoy Gateway has no native InferencePool backend).
#   - charts/inference-gateway: GatewayClass/Gateway/EnvoyProxy, the GIE body-based
#     router (BBR) as an ext_proc on the Gateway, one llm-d Endpoint Picker (EPP) +
#     InferencePool per entry of var.hyperpod_inference_gateway_schedulers (the same
#     {name, model_name, target_port} contract and inference/gateway-scheduler pod label
#     as the HyperPod gateway), the HTTPRoute, and PodMonitors inference-gateway-epp /
#     inference-gateway-bbr (job = <namespace>/<name>, matching the HyperPod gateway).
#
# Gated on var.enable_inference_gateway, which requires var.enable_inference_routing (the
# GIE CRDs in platform_inference_ext.tf) and excludes the HyperPod gateway. Images:
# docker.io + ghcr.io are vendored (images.tf); BBR comes from registry.k8s.io through
# the registry-k8s pull-through. Everything runs on the tainted system node group. No
# TLS / auth on the listener (in-VPC HTTP); the Envoy Service is ClusterIP.
#
# Pinned pair: Envoy Gateway v1.9.3 + Envoy AI Gateway v1.2.1 (its egMinVersion is
# 1.9.2) + llm-d EPP v1.0.0 + BBR v1.5.0 (the last BBR release). The EPP discovers the
# optional InferenceObjective/InferenceModelRewrite CRDs, so the vendored GIE v1.0.1 CRDs
# are enough. Live behavior (Gateway Programmed, a 200 through the gateway, the 75
# fixture metric names) is build checks V10/V11.

locals {
  inference_gateway_enabled = var.enable_inference_gateway

  inference_gateway_name   = "inference-gateway"
  envoy_gateway_namespace  = "envoy-gateway-system"
  ai_gateway_namespace     = "envoy-ai-gateway-system"
  ai_gateway_controller_sa = "ai-gateway-controller"
}

# Plan-time guard: the plain gateway needs the InferencePool CRDs and must not be
# combined with the HyperPod gateway (two Envoy Gateways, two sets of GIE CRDs).
resource "terraform_data" "inference_gateway_compat" {
  lifecycle {
    precondition {
      condition     = !var.enable_inference_gateway || var.enable_inference_routing
      error_message = "enable_inference_gateway requires enable_inference_routing (the InferencePool CRDs)."
    }
    precondition {
      condition     = !(var.enable_inference_gateway && var.enable_hyperpod_inference_gateway)
      error_message = "enable_inference_gateway and enable_hyperpod_inference_gateway are mutually exclusive."
    }
  }
}

# --- Envoy Gateway (controller + Gateway API / Envoy Gateway CRDs) ---
resource "helm_release" "envoy_gateway" {
  count = local.inference_gateway_enabled ? 1 : 0

  name             = "envoy-gateway"
  repository       = "oci://docker.io/envoyproxy"
  chart            = "gateway-helm"
  version          = var.envoy_gateway_chart_version
  namespace        = local.envoy_gateway_namespace
  create_namespace = true

  values = [yamlencode({
    global = {
      images = {
        envoyGateway = {
          image = "${aws_ecr_repository.vendored["envoy_gateway"].repository_url}:${local.vendored_tag}"
        }
        envoyProxy = {
          image = "${aws_ecr_repository.vendored["envoy_proxy"].repository_url}:${local.vendored_tag}"
        }
      }
    }
    deployment = {
      pod = {
        nodeSelector = local.system_node_selector
        tolerations  = [local.system_toleration]
      }
    }
    certgen = {
      job = {
        pod = {
          nodeSelector = local.system_node_selector
          tolerations  = [local.system_toleration]
        }
      }
    }
    # The Envoy AI Gateway integration values (its manifests/envoy-gateway-values.yaml
    # + examples/inference-pool/envoy-gateway-values-addon.yaml at v1.2.1).
    config = {
      envoyGateway = {
        gateway = { controllerName = "gateway.envoyproxy.io/gatewayclass-controller" }
        logging = { level = { default = "info" } }
        provider = {
          type       = "Kubernetes"
          kubernetes = { deploy = { type = "GatewayNamespace" } }
        }
        extensionApis = {
          enableEnvoyPatchPolicy = true
          enableBackend          = true
        }
        extensionManager = {
          hooks = {
            xdsTranslator = {
              translation = {
                listener = { includeAll = true }
                route    = { includeAll = true }
                cluster  = { includeAll = true }
                secret   = { includeAll = true }
              }
              post = ["Translation", "Cluster", "Route"]
            }
          }
          service = {
            fqdn = {
              hostname = "ai-gateway-controller.${local.ai_gateway_namespace}.svc.cluster.local"
              port     = 1063
            }
          }
          backendResources = [{
            group   = "inference.networking.k8s.io"
            kind    = "InferencePool"
            version = "v1"
          }]
        }
      }
    }
  })]

  depends_on = [
    terraform_data.inference_gateway_compat,
    helm_release.inference_extension_crds,
    null_resource.cluster_addons,
    null_resource.pullthrough_ready,
    null_resource.image_vendor,
    module.node_group,
  ]
}

# --- Envoy AI Gateway (CRDs + controller / extension server) ---
resource "helm_release" "ai_gateway_crds" {
  count = local.inference_gateway_enabled ? 1 : 0

  name             = "ai-gateway-crds"
  repository       = "oci://docker.io/envoyproxy"
  chart            = "ai-gateway-crds-helm"
  version          = var.envoy_ai_gateway_chart_version
  namespace        = local.ai_gateway_namespace
  create_namespace = true

  depends_on = [
    terraform_data.inference_gateway_compat,
    null_resource.cluster_addons,
    module.node_group,
  ]
}

resource "helm_release" "ai_gateway" {
  count = local.inference_gateway_enabled ? 1 : 0

  name             = "ai-gateway"
  repository       = "oci://docker.io/envoyproxy"
  chart            = "ai-gateway-helm"
  version          = var.envoy_ai_gateway_chart_version
  namespace        = local.ai_gateway_namespace
  create_namespace = true

  values = [yamlencode({
    controller = {
      image = {
        repository = aws_ecr_repository.vendored["ai_gateway_controller"].repository_url
        tag        = local.vendored_tag
      }
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
    }
    # The controller's mutating webhook injects this extproc sidecar into every Envoy
    # Gateway-managed Envoy pod, so it runs wherever the Envoy pods run.
    extProc = {
      image = {
        repository = aws_ecr_repository.vendored["ai_gateway_extproc"].repository_url
        tag        = local.vendored_tag
      }
    }
    envoyGateway = { namespace = local.envoy_gateway_namespace }
  })]

  depends_on = [
    helm_release.ai_gateway_crds,
    helm_release.envoy_gateway,
    null_resource.image_vendor,
  ]
}

# --- Gateway + BBR + EPPs + InferencePools + HTTPRoute + PodMonitors ---
resource "helm_release" "inference_gateway" {
  count = local.inference_gateway_enabled ? 1 : 0

  name      = "inference-gateway"
  chart     = "${path.module}/../charts/inference-gateway"
  namespace = kubernetes_namespace_v1.workload.metadata[0].name

  values = [yamlencode({
    gatewayName    = local.inference_gateway_name
    schedulerLabel = local.hyperpod_gateway_scheduler_label
    schedulers = [for s in var.hyperpod_inference_gateway_schedulers : {
      name       = s["name"]
      modelName  = s["model_name"]
      targetPort = tonumber(s["target_port"])
    }]
    envoy = {
      image = "${aws_ecr_repository.vendored["envoy_proxy"].repository_url}:${local.vendored_tag}"
    }
    epp = {
      image = "${aws_ecr_repository.vendored["llm_d_epp"].repository_url}:${local.vendored_tag}"
    }
    bbr = {
      image = "${local.ecr_registry}/registry-k8s/gateway-api-inference-extension/bbr:${var.inference_gateway_bbr_version}"
    }
    podDefaults = {
      nodeSelector = local.system_node_selector
      tolerations  = [local.system_toleration]
      # EPP/BBR are light control-plane-ish pods; the llm-d chart default (8 CPU / 8Gi)
      # cannot fit the m6i.xlarge system node group.
      resources = {
        requests = { cpu = "250m", memory = "512Mi" }
        limits   = { memory = "2Gi" }
      }
    }
    serviceMonitorLabel = { release = "kube-prometheus-stack" }
  })]

  set = [
    { name = "chartContentHash", value = local.chart_hashes["inference-gateway"] },
  ]

  depends_on = [
    helm_release.ai_gateway,
    helm_release.envoy_gateway,
    helm_release.inference_extension_crds,
    kubernetes_namespace_v1.workload,
    # PodMonitor CRD.
    helm_release.kube_prometheus_stack,
  ]
}
