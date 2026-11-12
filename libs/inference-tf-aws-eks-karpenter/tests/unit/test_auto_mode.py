"""Contract tests for the EKS Auto Mode variant, the plain inference gateway and the deploy role.

Scope: invariants `terraform validate` cannot catch and whose drift is silent —
- Auto Mode OFF is a no-op: the karpenter/storage charts and the destroy scripts render
  byte-identically to the pre-Auto-Mode base (golden files), chart values that only exist
  for Auto Mode are not even set, and every resource that gained a count has a `moved`
  block so existing state keeps its addresses;
- Auto Mode ON: no Karpenter controller / SQS / device plugin, NodeClasses +
  eks.amazonaws.com keys, no g7, capacity type pinned, Drifted: 0, EC2 access entry;
- the plain gateway's PodMonitor names (they fix the Prometheus `job` label catalog-demo
  reads) and its image supply;
- the deploy role is namespaced RBAC only (never cluster scope, never RGDs).
Live behavior is the build plan's checks V1-V13.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

import hcl2
import yaml

from inference_tf_aws_eks_karpenter.template import TEMPLATE_PATH

ENGINE = TEMPLATE_PATH / "engine"
CHARTS = TEMPLATE_PATH / "charts"
GOLDEN = Path(__file__).parent / "golden"

KARPENTER_BASE_SETS = {"gpuAmiId": "a", "nodeInstanceProfile": "p", "discoveryTag": "t"}
AUTO_SETS = {"autoMode.enabled": "true", "autoMode.nodeRoleName": "auto-node"}


def _helm(chart: str, values: dict[str, str], *extra: str) -> str:
    args = ["helm", "template", str(CHARTS / chart)]
    for k, v in values.items():
        args += ["--set", f"{k}={v}"]
    return subprocess.run([*args, *extra], check=True, capture_output=True, text=True).stdout


def _docs(text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(text) if d]


def _block(content: str, header_re: str) -> str:
    start = re.search(header_re + r"\s*\{", content)
    assert start is not None, f"block {header_re!r} not found"
    depth, idx = 1, start.end()
    while idx < len(content) and depth > 0:
        depth += {"{": 1, "}": -1}.get(content[idx], 0)
        idx += 1
    return content[start.end() : idx - 1]


def _resource(content: str, type_: str, name: str) -> str:
    return _block(content, rf'resource\s+"{re.escape(type_)}"\s+"{re.escape(name)}"')


def _engine_text() -> str:
    return "\n".join(p.read_text() for p in sorted(ENGINE.glob("*.tf")))


def _unquote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    if isinstance(value, list):
        return [_unquote(v) for v in value]
    if isinstance(value, dict):
        return {_unquote(k): _unquote(v) for k, v in value.items()}
    return value


def _defaults() -> dict[str, Any]:
    with open(ENGINE / "presets" / "defaults-all.tfvars") as f:
        return dict(_unquote(hcl2.load(f)))


def _strip_tftpl_if(text: str, var: str, keep: bool) -> str:
    """Evaluate `%{ if <var> ~}...%{ endif ~}` blocks the way templatefile does (~ strips the newline)."""
    pattern = re.compile(rf"%\{{ if {var} ~\}}\n(.*?)%\{{ endif ~\}}\n", re.DOTALL)
    return pattern.sub(lambda m: m.group(1) if keep else "", text)


class TestAutoModeOffIsUnchanged(unittest.TestCase):
    """With enable_auto_mode=false (the default) nothing an existing user deploys changes."""

    CASES = {
        "karpenter-default.yaml": {},
        "karpenter-spot.yaml": {"gpuSpot.enabled": "true"},
        "karpenter-nop.yaml": {"gpuP.enabled": "false", "gpuParallelPull.enabled": "false"},
        "karpenter-odcr.yaml": {"gpuP.capacityReservationId": "cr-1", "gpuSpot.enabled": "true"},
    }

    def test_karpenter_chart_renders_byte_identical_to_base(self) -> None:
        for golden, values in self.CASES.items():
            with self.subTest(golden=golden):
                rendered = _helm("karpenter", {**KARPENTER_BASE_SETS, **values})
                self.assertEqual(rendered, (GOLDEN / f"{golden}.golden").read_text())

    def test_storage_chart_renders_byte_identical_to_base(self) -> None:
        self.assertEqual(_helm("storage", {}), (GOLDEN / "storage-default.yaml.golden").read_text())

    def test_destroy_scripts_render_identical_when_off(self) -> None:
        for script in ("local-destroy-cleanup.sh.tftpl", "post-eks-vpc-cleanup.sh.tftpl"):
            with self.subTest(script=script):
                current = _strip_tftpl_if((ENGINE / script).read_text(), "auto_mode", keep=False)
                self.assertEqual(current, (GOLDEN / f"{script}.golden").read_text())

    def test_auto_mode_only_chart_values_are_not_set_when_off(self) -> None:
        """No `autoMode.*` / `ebs.autoMode` set entry when off, so helm_release values are unchanged."""
        karpenter = _resource((ENGINE / "platform_karpenter.tf").read_text(), "helm_release", "karpenter_nodepools")
        self.assertRegex(
            karpenter, r"var\.enable_auto_mode \? \[\s*#[^\n]*\n(?:\s*#[^\n]*\n)*\s*\{ name = \"autoMode\.enabled\""
        )
        self.assertRegex(karpenter, r"\] : \[\]\)")
        storage = _resource((ENGINE / "platform_storage.tf").read_text(), "helm_release", "storage")
        self.assertIn('var.enable_auto_mode ? [{ name = "ebs.autoMode", value = "true" }] : []', storage)

    def test_newly_counted_resources_have_moved_blocks(self) -> None:
        """A count added for Auto Mode must not destroy/recreate existing deployments' resources."""
        text = _engine_text()
        moved = set(re.findall(r"moved\s*\{\s*from\s*=\s*([\w.]+)\s*to\s*=\s*([\w.]+)\[0\]", text))
        self.assertTrue(all(src == dst for src, dst in moved))
        expected = {
            "helm_release.karpenter",
            "aws_sqs_queue.karpenter_interruption",
            "aws_sqs_queue_policy.karpenter_interruption",
            "aws_eks_pod_identity_association.karpenter",
            "helm_release.nvidia_device_plugin",
            "module.karpenter_controller_role",
            "aws_iam_role_policy.karpenter_controller",
        }
        self.assertEqual({src for src, _ in moved}, expected)

    def test_defaults_keep_existing_behavior(self) -> None:
        d = _defaults()
        self.assertIs(d["enable_auto_mode"], False)
        self.assertEqual(d["public_access_cidrs"], ["0.0.0.0/0"])
        self.assertIs(d["enable_inference_gateway"], False)
        self.assertEqual(d["deploy_role_arn"], "")
        self.assertEqual(d["permissions_boundary_arn"], "")
        self.assertIs(d["enable_jsc_cluster_tag"], False)

    def test_cluster_role_policy_order_is_append_only(self) -> None:
        """AmazonEKSClusterPolicy stays attachment key "0"; Auto Mode policies are appended after it."""
        role = _block((ENGINE / "iam.tf").read_text(), r'module\s+"cluster_role"')
        first = role.index("AmazonEKSClusterPolicy")
        for policy in (
            "AmazonEKSComputePolicy",
            "AmazonEKSBlockStoragePolicyV2",
            "AmazonEKSLoadBalancingPolicy",
            "AmazonEKSNetworkingPolicy",
        ):
            self.assertGreater(role.index(policy), first)


class TestAutoModeCluster(unittest.TestCase):
    def test_public_access_cidrs_comes_from_the_variable(self) -> None:
        main = (ENGINE / "main.tf").read_text()
        cluster = _block(main, r'module\s+"eks_cluster"')
        self.assertIn("public_access_cidrs = var.public_access_cidrs", cluster)
        self.assertNotRegex(cluster, r"public_access_cidrs\s*=\s*\[")

    def test_compute_storage_elb_configs_enabled_together_without_builtin_pools(self) -> None:
        cluster = _resource((ENGINE / "modules" / "eks_cluster" / "main.tf").read_text(), "aws_eks_cluster", "this")
        for block in ("compute_config", "storage_config", "kubernetes_network_config"):
            self.assertRegex(cluster, rf'dynamic "{block}" \{{\s*for_each = var\.enable_auto_mode \? \[1\] : \[\]')
        compute = _block(cluster, r'dynamic\s+"compute_config"')
        self.assertRegex(compute, r"node_pools\s*=\s*\[\]")
        self.assertNotIn("node_role_arn", compute.split("content")[1])

    def test_cluster_trust_adds_tag_session_only_on_auto_mode(self) -> None:
        trust = _block((ENGINE / "iam.tf").read_text(), r'data\s+"aws_iam_policy_document"\s+"eks_trust"')
        self.assertIn('var.enable_auto_mode ? ["sts:AssumeRole", "sts:TagSession"] : ["sts:AssumeRole"]', trust)

    def test_auto_node_role_has_its_own_ec2_access_entry(self) -> None:
        main = (ENGINE / "main.tf").read_text()
        entry = _resource(main, "aws_eks_access_entry", "auto_node")
        self.assertIn('type          = "EC2"', entry)
        self.assertIn("module.auto_node_role[0].role_arn", entry)
        assoc = _resource(main, "aws_eks_access_policy_association", "auto_node")
        self.assertIn("AmazonEKSAutoNodePolicy", assoc)
        role = _block((ENGINE / "iam.tf").read_text(), r'module\s+"auto_node_role"')
        self.assertIn("AmazonEKSWorkerNodeMinimalPolicy", role)
        self.assertIn("AmazonEC2ContainerRegistryPullOnly", role)
        addons = _resource((ENGINE / "eks_addons.tf").read_text(), "null_resource", "cluster_addons")
        self.assertIn("aws_eks_access_entry.auto_node", addons)
        self.assertIn("aws_eks_access_policy_association.auto_node", addons)

    def test_karpenter_sqs_and_device_plugin_off_on_auto_mode(self) -> None:
        karpenter = (ENGINE / "platform_karpenter.tf").read_text()
        for type_, name in (
            ("helm_release", "karpenter"),
            ("aws_sqs_queue", "karpenter_interruption"),
            ("aws_sqs_queue_policy", "karpenter_interruption"),
            ("aws_eks_pod_identity_association", "karpenter"),
        ):
            self.assertIn("count = var.enable_auto_mode ? 0 : 1", _resource(karpenter, type_, name), f"{type_}.{name}")
        for name in ("karpenter",):
            for type_ in ("aws_cloudwatch_event_rule", "aws_cloudwatch_event_target"):
                self.assertIn("var.enable_auto_mode ? {} :", _resource(karpenter, type_, name))
        gpu = (ENGINE / "platform_gpu.tf").read_text()
        self.assertIn("count = var.enable_auto_mode ? 0 : 1", _resource(gpu, "helm_release", "nvidia_device_plugin"))

    def test_hyperpod_addon_rejected_on_auto_mode(self) -> None:
        check = _resource((ENGINE / "main.tf").read_text(), "terraform_data", "auto_mode_compat")
        self.assertIn("!(var.enable_auto_mode && var.enable_hyperpod_inference_gateway)", check)

    def test_dcgm_selects_nvidia_nodes_by_auto_mode_label(self) -> None:
        dcgm = _resource((ENGINE / "platform_prometheus.tf").read_text(), "helm_release", "dcgm_exporter")
        self.assertIn("eks.amazonaws.com/instance-gpu-manufacturer", dcgm)
        self.assertIn('value = "nvidia"', dcgm)

    def test_cleanup_scripts_include_managed_resources_on_auto_mode(self) -> None:
        drain = _strip_tftpl_if((ENGINE / "local-destroy-cleanup.sh.tftpl").read_text(), "auto_mode", keep=True)
        self.assertEqual(
            drain.count("--include-managed-resources"), 2
        )  # describe-instances + describe-network-interfaces
        post = _strip_tftpl_if((ENGINE / "post-eks-vpc-cleanup.sh.tftpl").read_text(), "auto_mode", keep=True)
        self.assertIn("--include-managed-resources", post)


class TestAutoModeNodePools(unittest.TestCase):
    docs: list[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = _docs(_helm("karpenter", {**KARPENTER_BASE_SETS, **AUTO_SETS, "gpuSpot.enabled": "true"}))

    def _pools(self) -> dict[str, dict[str, Any]]:
        return {d["metadata"]["name"]: d for d in self.docs if d["kind"] == "NodePool"}

    def test_two_nodeclasses_and_no_ec2nodeclass(self) -> None:
        classes = [d for d in self.docs if d["kind"] == "NodeClass"]
        self.assertEqual({d["metadata"]["name"] for d in classes}, {"cpu", "gpu"})
        self.assertTrue(all(d["apiVersion"] == "eks.amazonaws.com/v1" for d in classes))
        self.assertTrue(all(d["spec"]["role"] == "auto-node" for d in classes))
        self.assertFalse([d for d in self.docs if d["kind"] == "EC2NodeClass"])

    def test_pools_use_eks_keys_and_nodeclass_ref(self) -> None:
        pools = self._pools()
        self.assertEqual(set(pools), {"cpu", "gpu-g", "gpu-p", "gpu-spot"})
        for name, pool in pools.items():
            spec = pool["spec"]["template"]["spec"]
            self.assertEqual(spec["nodeClassRef"]["group"], "eks.amazonaws.com", name)
            self.assertEqual(spec["nodeClassRef"]["kind"], "NodeClass", name)
            keys = [r["key"] for r in spec["requirements"]]
            self.assertFalse([k for k in keys if k.startswith("karpenter.k8s.aws/")], name)
            self.assertIn("karpenter.sh/capacity-type", keys, f"{name}: capacity type must be pinned")
            self.assertEqual(spec["expireAfter"], "336h")

    def test_g7_dropped_from_every_pool(self) -> None:
        for name, pool in self._pools().items():
            for req in pool["spec"]["template"]["spec"]["requirements"]:
                if req["key"] == "eks.amazonaws.com/instance-family":
                    self.assertNotIn("g7", req["values"], name)
                    if name == "gpu-g":
                        self.assertEqual(req["values"], ["g5", "g6", "g6e", "g7e"])

    def test_gpu_pools_never_disrupted_for_drift(self) -> None:
        for name in ("gpu-g", "gpu-p", "gpu-spot"):
            disruption = self._pools()[name]["spec"]["disruption"]
            self.assertEqual(disruption["consolidationPolicy"], "WhenEmpty", name)
            self.assertIn({"nodes": "0", "reasons": ["Drifted"]}, disruption["budgets"], name)

    def test_labels_and_taints_match_karpenter_mode(self) -> None:
        karpenter = {
            d["metadata"]["name"]: d
            for d in _docs(_helm("karpenter", {**KARPENTER_BASE_SETS, "gpuSpot.enabled": "true"}))
            if d["kind"] == "NodePool"
        }
        for name, pool in self._pools().items():
            for field in ("taints",):
                self.assertEqual(
                    pool["spec"]["template"]["spec"].get(field),
                    karpenter[name]["spec"]["template"]["spec"].get(field),
                    name,
                )
            self.assertEqual(
                pool["spec"]["template"].get("metadata"), karpenter[name]["spec"]["template"].get("metadata"), name
            )
            self.assertEqual(pool["spec"]["limits"], karpenter[name]["spec"]["limits"], name)

    def test_auto_mode_rejects_odcr(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError):
            _helm("karpenter", {**KARPENTER_BASE_SETS, **AUTO_SETS, "gpuP.capacityReservationId": "cr-1"})

    def test_storage_auto_class_is_default(self) -> None:
        classes = {
            d["metadata"]["name"]: d
            for d in _docs(_helm("storage", {"ebs.autoMode": "true"}))
            if d["kind"] == "StorageClass"
        }
        self.assertEqual(classes["ebs-gp3-auto"]["provisioner"], "ebs.csi.eks.amazonaws.com")
        self.assertEqual(
            classes["ebs-gp3-auto"]["metadata"]["annotations"]["storageclass.kubernetes.io/is-default-class"], "true"
        )
        self.assertEqual(
            classes["ebs-gp3"]["metadata"]["annotations"]["storageclass.kubernetes.io/is-default-class"], "false"
        )


class TestPlainInferenceGateway(unittest.TestCase):
    docs: list[dict[str, Any]]

    @classmethod
    def setUpClass(cls) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            values = Path(tmp) / "values.yaml"
            values.write_text(
                "schedulers:\n  - {name: default, modelName: gemma, targetPort: 8000}\n"
                "  - {name: second, modelName: m2, targetPort: 8001}\n"
            )
            cls.docs = _docs(_helm("inference-gateway", {}, "-n", "inference", "-f", str(values)))

    def _kind(self, kind: str) -> list[dict[str, Any]]:
        return [d for d in self.docs if d["kind"] == kind]

    def test_podmonitor_names_fix_the_job_label(self) -> None:
        """job = <namespace>/<PodMonitor name> must be inference/inference-gateway-{epp,bbr}."""
        monitors = {(d["metadata"]["namespace"], d["metadata"]["name"]) for d in self._kind("PodMonitor")}
        self.assertEqual(monitors, {("inference", "inference-gateway-epp"), ("inference", "inference-gateway-bbr")})
        for pm in self._kind("PodMonitor"):
            self.assertEqual(pm["metadata"]["labels"], {"release": "kube-prometheus-stack"})

    def test_one_pool_and_epp_per_scheduler(self) -> None:
        pools = {d["metadata"]["name"]: d for d in self._kind("InferencePool")}
        self.assertEqual(set(pools), {"default", "second"})
        self.assertEqual(pools["second"]["spec"]["selector"]["matchLabels"], {"inference/gateway-scheduler": "second"})
        self.assertEqual(pools["second"]["spec"]["endpointPickerRef"]["name"], "second-epp")
        self.assertEqual(
            {d["metadata"]["name"] for d in self._kind("Deployment")},
            {"default-epp", "second-epp", "inference-gateway-bbr"},
        )

    def test_route_matches_bbr_header_with_first_pool_fallback(self) -> None:
        (route,) = self._kind("HTTPRoute")
        rules = route["spec"]["rules"]
        self.assertEqual(
            rules[0]["matches"][0]["headers"][0], {"type": "Exact", "name": "X-Gateway-Model-Name", "value": "gemma"}
        )
        self.assertEqual(rules[-1]["backendRefs"][0]["name"], "default")
        self.assertNotIn("headers", rules[-1]["matches"][0])
        (policy,) = self._kind("EnvoyExtensionPolicy")
        self.assertEqual(policy["spec"]["extProc"][0]["processingMode"], {"request": {"body": "Buffered"}})

    def test_gateway_gated_on_routing_and_exclusive_with_hyperpod(self) -> None:
        tf = (ENGINE / "platform_inference_gateway.tf").read_text()
        check = _resource(tf, "terraform_data", "inference_gateway_compat")
        self.assertIn("!var.enable_inference_gateway || var.enable_inference_routing", check)
        self.assertIn("!(var.enable_inference_gateway && var.enable_hyperpod_inference_gateway)", check)
        for name in ("envoy_gateway", "ai_gateway_crds", "ai_gateway", "inference_gateway"):
            self.assertIn("count = local.inference_gateway_enabled ? 1 : 0", _resource(tf, "helm_release", name), name)

    def test_gateway_images_vendored_and_bbr_pulled_through(self) -> None:
        images = (ENGINE / "images.tf").read_text()
        for key, source in (
            ("envoy_gateway", "docker.io/envoyproxy/gateway:"),
            ("ai_gateway_controller", "docker.io/envoyproxy/ai-gateway-controller:"),
            ("ai_gateway_extproc", "docker.io/envoyproxy/ai-gateway-extproc:"),
            ("llm_d_epp", "ghcr.io/llm-d/llm-d-router-endpoint-picker:"),
        ):
            self.assertRegex(
                images, rf"{key} = \{{\s*repo\s*=\s*\"vendored/[^\"]+\"\s*source\s*=\s*\"{re.escape(source)}"
            )
        tf = (ENGINE / "platform_inference_gateway.tf").read_text()
        self.assertIn("${local.ecr_registry}/registry-k8s/gateway-api-inference-extension/bbr:", tf)
        self.assertNotIn("docker.io/envoyproxy", _resource(tf, "helm_release", "inference_gateway"))


class TestDeployRole(unittest.TestCase):
    tf = (ENGINE / "deploy_access.tf").read_text()

    def test_access_entry_is_group_bound_and_gated(self) -> None:
        entry = _resource(self.tf, "aws_eks_access_entry", "deploy")
        self.assertIn("count = local.deploy_enabled ? 1 : 0", entry)
        self.assertIn("kubernetes_groups = [local.deploy_group]", entry)
        self.assertIn('deploy_group   = "jsc-deploy"', self.tf)

    def test_edit_policy_optional_and_namespace_scoped(self) -> None:
        assoc = _resource(self.tf, "aws_eks_access_policy_association", "deploy_edit")
        self.assertIn("var.deploy_role_edit_policy", assoc)
        self.assertIn('type       = "namespace"', assoc)
        self.assertNotIn("ClusterAdmin", self.tf)

    def test_rbac_is_namespaced_only(self) -> None:
        self.assertNotRegex(self.tf, r'resource\s+"kubernetes_cluster_role')
        role = _resource(self.tf, "kubernetes_role_v1", "deploy")
        self.assertNotIn("resourcegraphdefinitions", role)
        self.assertIn("api_groups = var.deploy_kro_api_groups", role)
        for resource in ("inferencepools", "podmonitors"):
            self.assertIn(resource, role)


class TestProvisioningHooks(unittest.TestCase):
    def test_every_iam_role_gets_the_permissions_boundary(self) -> None:
        text = _engine_text()
        calls = re.findall(
            r'source\s*=\s*"\./modules/(?:iam_role|codebuild_job)"\s*\n\s*permissions_boundary\s*=\s*local\.permissions_boundary',
            text,
        )
        sources = re.findall(r'source\s*=\s*"\./modules/(?:iam_role|codebuild_job)"', text)
        self.assertEqual(len(calls), len(sources))
        self.assertNotRegex(text, r'resource\s+"aws_iam_role"\s')  # root creates roles only through the modules
        for module in ("iam_role", "codebuild_job"):
            self.assertIn(
                "permissions_boundary = var.permissions_boundary", (ENGINE / "modules" / module / "main.tf").read_text()
            )

    def test_jsc_cluster_tag_is_opt_in(self) -> None:
        self.assertIn(
            'var.enable_jsc_cluster_tag ? { "jsc:cluster" = local.cluster_name } : {}', (ENGINE / "main.tf").read_text()
        )

    def test_partial_s3_backend_is_opt_in(self) -> None:
        example = (ENGINE / "backend-s3.tf.example").read_text()
        self.assertRegex(example, r'backend "s3" \{\}')
        self.assertFalse((ENGINE / "backend.tf").exists(), "a shipped backend.tf would break jd's local-state flow")

    def test_lb_cleanup_runs_before_lb_owners_and_fails_loudly(self) -> None:
        tf = (ENGINE / "platform_lb_cleanup.tf").read_text()
        cleanup = _resource(tf, "null_resource", "lb_cleanup")
        for owner in (
            "aws_eks_addon.hyperpod_inference",
            "helm_release.inference_gateway",
            "helm_release.hyperpod_gateway",
        ):
            self.assertIn(owner, cleanup)
        self.assertIn("exit 1", cleanup)
        self.assertIn("ignore_changes = [triggers]", cleanup)
        self.assertIn('.tagging"', _resource(tf, "aws_vpc_endpoint", "tagging"))


class TestResourceInventory(unittest.TestCase):
    def test_inventory_lists_every_root_resource(self) -> None:
        """docs/resources.json (scripts/resource_inventory.py) must be regenerated when a resource is added."""
        inventory = json.loads((TEMPLATE_PATH.parent.parent / "docs" / "resources.json").read_text())
        listed = {r["address"] for r in inventory["resources"]}
        declared = set()
        for tf in ENGINE.glob("*.tf"):
            declared |= {
                f"{t}.{n}" for t, n in re.findall(r'^resource\s+"([\w-]+)"\s+"([\w-]+)"', tf.read_text(), re.M)
            }
        self.assertEqual(sorted(declared - listed), [], "run scripts/resource_inventory.py")
