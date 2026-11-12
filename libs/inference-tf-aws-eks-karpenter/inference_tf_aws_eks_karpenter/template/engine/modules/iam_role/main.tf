resource "aws_iam_role" "this" {
  name                 = var.role_name
  assume_role_policy   = var.assume_role_policy
  permissions_boundary = var.permissions_boundary
  tags                 = var.combined_tags
}

# Each attachment references aws_iam_role.this.name, so Terraform always destroys
# the attachments BEFORE the role itself — which AWS requires (a role cannot be
# deleted while policies are still attached).
#
# Note what this edge does NOT do: it does not order the attachments relative to
# whatever CONSUMES this role (e.g. an EKS node group built on node_role). A
# consumer that only references the `role_arn` output depends on the role, not on
# these attachments — they are siblings, unordered against each other. On destroy
# Terraform is then free to remove an attachment while the consumer is still live,
# stripping (for the node role) the worker/CNI permissions out from under running
# nodes. The `role_arn` output below closes that gap.
resource "aws_iam_role_policy_attachment" "policies" {
  for_each   = { for idx, arn in var.policy_arns : tostring(idx) => arn }
  role       = aws_iam_role.this.name
  policy_arn = each.value
}
