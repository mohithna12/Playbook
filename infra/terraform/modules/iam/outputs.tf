output "eks_cluster_role_arn" {
  value = aws_iam_role.eks_cluster.arn
}

output "eks_node_role_arn" {
  value = aws_iam_role.eks_node.arn
}

output "rds_monitoring_role_arn" {
  value = aws_iam_role.rds_monitoring.arn
}

output "app_pod_role_arn" {
  value = aws_iam_role.app_pod.arn
}
