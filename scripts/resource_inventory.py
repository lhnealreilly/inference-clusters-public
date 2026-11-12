"""Generate the tf-aws-eks-karpenter resource inventory (docs/RESOURCES.md + docs/resources.json).

Every AWS / Kubernetes / Helm object the template's Terraform can create, with its type, the
variables that gate it, and the name pattern AWS sees. dev-2 scopes the provisioning role
(JscProvisionRole) by these names, so the inventory must cover every resource whether or not
an opt-in is on. Source of truth = the engine HCL (parsed with python-hcl2, no AWS access).

Optionally pass the text output of a `terraform plan` (one or more `--plan-log NAME=PATH`):
each listed address is then marked as created in that configuration, which cross-checks the
static parse against a real plan.

    uv run python scripts/resource_inventory.py \
        --plan-log auto-mode=/tmp/auto-plan/engine/a.log
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

import hcl2

PKG = Path(__file__).resolve().parent.parent / "libs" / "inference-tf-aws-eks-karpenter"
ENGINE = PKG / "inference_tf_aws_eks_karpenter" / "template" / "engine"
DOCS = PKG / "docs"

# Attributes that carry the name AWS (or Kubernetes) sees, in priority order.
NAME_ATTRS = (
    "name",
    "name_prefix",
    "bucket",
    "bucket_prefix",
    "role_name",
    "bucket_name_prefix",
    "project_name",
    "node_group_name",
    "cluster_name",
    "repository_name",
    "service_name",
    "key",
)

# Readable placeholders for the template's naming locals.
SUBSTITUTIONS = (
    ("${local.resource_name_prefix}", "{cluster}"),
    ("${local.cluster_name}", "{cluster}"),
    ("${var.cluster_name}", "{cluster}"),
    ("${module.eks_cluster.cluster_name}", "{cluster}"),
    ("${random_id.postfix.hex}", "{postfix}"),
    ("${data.aws_region.current.id}", "{region}"),
    ("${data.aws_caller_identity.current.account_id}", "{account}"),
    ("${data.aws_partition.current.partition}", "{partition}"),
)

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _unquote(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _pattern(value: Any) -> str:
    text = str(_unquote(value))
    for old, new in SUBSTITUTIONS:
        text = text.replace(old, new)
    return text


def _gate(body: dict[str, Any]) -> str:
    for key in ("count", "for_each"):
        if key in body:
            expr = str(_unquote(body[key]))
            if expr.startswith("${") and expr.endswith("}"):
                expr = expr[2:-1]
            return f"{key} = {expr}"
    return "always"


# Types whose identifying attribute is not a name.
TYPE_NAME_ATTR = {
    "aws_eks_access_entry": "principal_arn",
    "aws_eks_access_policy_association": "policy_arn",
    "aws_eks_addon": "addon_name",
    "aws_eks_pod_identity_association": "service_account",
    "aws_iam_role_policy_attachment": "policy_arn",
}


def _name(body: dict[str, Any], kind: str) -> str:
    if kind in TYPE_NAME_ATTR and TYPE_NAME_ATTR[kind] in body:
        return _pattern(body[TYPE_NAME_ATTR[kind]])
    for attr in NAME_ATTRS:
        if attr in body:
            return _pattern(body[attr])
    if kind.startswith("kubernetes_") and "metadata" in body:
        meta = body["metadata"][0] if isinstance(body["metadata"], list) else body["metadata"]
        if "name" in meta:
            ns = _pattern(meta.get("namespace", "")) if "namespace" in meta else ""
            return f"{ns}/{_pattern(meta['name'])}" if ns else _pattern(meta["name"])
    return ""


def _blocks(directory: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resources: list[dict[str, Any]] = []
    modules: list[dict[str, Any]] = []
    for tf in sorted(directory.glob("*.tf")):
        with open(tf) as f:
            parsed = hcl2.load(f)
        for entry in parsed.get("resource", []):
            for rtype, named in entry.items():
                for rname, body in named.items():
                    resources.append({"type": _unquote(rtype), "name": _unquote(rname), "file": tf.name, "body": body})
        for entry in parsed.get("module", []):
            for mname, body in entry.items():
                modules.append({"name": _unquote(mname), "file": tf.name, "body": body})
    return resources, modules


def _module_resources(source: str) -> list[dict[str, Any]]:
    resources, _ = _blocks(ENGINE / source.removeprefix("./"))
    return resources


def build() -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    resources, modules = _blocks(ENGINE)
    for r in resources:
        inventory.append(
            {
                "address": f"{r['type']}.{r['name']}",
                "type": r["type"],
                "provider": r["type"].split("_", 1)[0],
                "file": r["file"],
                "gate": _gate(r["body"]),
                "name_pattern": _name(r["body"], r["type"]),
            }
        )
    for m in modules:
        source = str(_unquote(m["body"]["source"]))
        call_name = ""
        for attr in (
            "role_name",
            "bucket_name_prefix",
            "project_name",
            "node_group_name",
            "cluster_name",
            "resource_name_prefix",
        ):
            if attr in m["body"]:
                call_name = _pattern(m["body"][attr])
                break
        for r in _module_resources(source):
            own = _name(r["body"], r["type"])
            # Inside a module the name usually derives from the call's argument.
            pattern = own
            for var in ("role_name", "bucket_name_prefix", "project_name", "node_group_name", "cluster_name"):
                pattern = pattern.replace("${var." + var + "}", call_name)
            inventory.append(
                {
                    "address": f"module.{m['name']}.{r['type']}.{r['name']}",
                    "type": r["type"],
                    "provider": r["type"].split("_", 1)[0],
                    "file": f"{m['file']} -> {source.removeprefix('./')}/{r['file']}",
                    "gate": _gate(m["body"])
                    if _gate(r["body"]) == "always"
                    else f"{_gate(m['body'])}; {_gate(r['body'])}",
                    "name_pattern": pattern,
                }
            )
    return sorted(inventory, key=lambda e: (e["provider"], e["type"], e["address"]))


def _planned(log: Path) -> set[str]:
    addresses = set()
    for line in log.read_text().splitlines():
        m = re.match(r"\s*# (\S+) will be created", ANSI.sub("", line))
        if m:
            addresses.add(re.sub(r"\[[^\]]*\]", "", m.group(1)))
    return addresses


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plan-log", action="append", default=[], help="NAME=PATH of a `terraform plan` text log")
    args = parser.parse_args()

    inventory = build()
    plans: dict[str, set[str]] = {}
    for spec in args.plan_log:
        label, _, path = spec.partition("=")
        plans[label] = _planned(Path(path))
    for entry in inventory:
        entry["in_plan"] = {label: entry["address"] in planned for label, planned in plans.items()}
    known = {e["address"] for e in inventory}
    unknown = {label: sorted(p - known) for label, p in plans.items() if p - known}
    if unknown:
        raise SystemExit(f"plan addresses missing from the static inventory: {unknown}")

    DOCS.mkdir(exist_ok=True)
    (DOCS / "resources.json").write_text(
        json.dumps(
            {
                "placeholders": {"{cluster}": "<cluster_name_prefix>-<8 hex random_id>"},
                "plans": sorted(plans),
                "resources": inventory,
            },
            indent=2,
        )
        + "\n"
    )

    by_type: dict[str, list[dict[str, Any]]] = {}
    for e in inventory:
        by_type.setdefault(e["type"], []).append(e)
    lines = [
        "<!-- Generated by scripts/resource_inventory.py; do not edit by hand. -->",
        "",
        "# Resource inventory: tf-aws-eks-karpenter",
        "",
        "Every resource the template's Terraform can create, with the variables that gate it and the",
        "name AWS / Kubernetes sees. `{cluster}` = `<cluster_name_prefix>-<8 hex random_id>` (the EKS",
        "cluster name, also the prefix of every named resource); `{region}`, `{account}`, `{partition}`",
        "as usual. Use it to scope a provisioning role by name. Resources Terraform does not own",
        "(pull-through ECR repos `ecr-public/*`, `quay/*`, `registry-k8s/*` created on first pull;",
        "Karpenter / Auto Mode instances, volumes and ENIs; load balancers created by in-cluster",
        "controllers; the EKS-created `eks-cluster-sg-{cluster}-*` security group) are listed at the end.",
        "",
    ]
    if plans:
        lines += [
            "`plan` column: created in the read-only plan(s) " + ", ".join(f"`{p}`" for p in sorted(plans)) + ".",
            "",
        ]
    lines += [f"Totals: {len(inventory)} resource blocks, {len(by_type)} types.", ""]
    header = "| Address | Gate | Name pattern |" + (" Plan |" if plans else "")
    sep = "|---|---|---|" + ("---|" if plans else "")
    for rtype in sorted(by_type, key=lambda t: (t.split("_", 1)[0], t)):
        lines += [f"## `{rtype}`", "", header, sep]
        for e in by_type[rtype]:
            row = f"| `{e['address']}` | {e['gate'].replace('|', '\\|')} | `{e['name_pattern'] or '-'}` |"
            if plans:
                row += " " + ", ".join(p for p, hit in e["in_plan"].items() if hit) + " |"
            lines.append(row)
        lines.append("")
    lines += [
        "## Created outside Terraform state",
        "",
        "| What | Created by | Name / tag |",
        "|---|---|---|",
        "| ECR pull-through repos + rules/templates | ECR on first pull; `pullthrough.tf` ensure script"
        " (account-regional singletons) | `ecr-public/*`, `quay/*`, `registry-k8s/*` |",
        "| EC2 instances, EBS volumes, ENIs (inference nodes) | Karpenter or EKS Auto Mode"
        " | tags `karpenter.sh/nodepool`, `eks:eks-cluster-name={cluster}` |",
        "| EKS cluster security group | EKS | `eks-cluster-sg-{cluster}-*` |",
        "| Load balancers + target groups | Auto Mode / in-cluster LBC | in the template VPC |",
        "| Workload ECR repos | onboarder / image-build CodeBuild | `{cluster}/workload/*` |",
        "",
    ]
    (DOCS / "RESOURCES.md").write_text("\n".join(lines))
    print(f"wrote {len(inventory)} resources to {DOCS}")


if __name__ == "__main__":
    main()
