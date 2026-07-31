################################################################################
# EKS Cluster + Node Groups
################################################################################

resource "aws_eks_cluster" "main" {
  name     = "${var.project}-${var.environment}"
  version  = var.cluster_version
  role_arn = var.cluster_role_arn

  vpc_config {
    subnet_ids              = var.private_app_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    security_group_ids      = [aws_security_group.cluster.id]
  }

  tags = var.tags
}

################################################################################
# Cluster Security Group
################################################################################

resource "aws_security_group" "cluster" {
  name_prefix = "${var.project}-eks-cluster-"
  vpc_id      = var.vpc_id
  description = "EKS cluster security group"

  tags = merge(var.tags, {
    Name = "${var.project}-eks-cluster-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group_rule" "cluster_egress" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.cluster.id
  description       = "Allow all outbound"
}

################################################################################
# General Node Group (on-demand, t4g.medium)
################################################################################

resource "aws_eks_node_group" "general" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.project}-general"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.private_app_subnet_ids
  instance_types  = [var.general_instance_type]
  ami_type        = "AL2023_ARM_64_STANDARD"
  capacity_type   = "ON_DEMAND"

  scaling_config {
    desired_size = var.general_desired_size
    min_size     = var.general_min_size
    max_size     = var.general_max_size
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    role = "general"
  }

  tags = var.tags
}

################################################################################
# Compute Node Group (spot, c7g.large, for simulation workers)
################################################################################

resource "aws_eks_node_group" "compute" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "${var.project}-compute"
  node_role_arn   = var.node_role_arn
  subnet_ids      = var.private_app_subnet_ids
  instance_types  = [var.compute_instance_type]
  ami_type        = "AL2023_ARM_64_STANDARD"
  capacity_type   = "SPOT"

  scaling_config {
    desired_size = var.compute_desired_size
    min_size     = var.compute_min_size
    max_size     = var.compute_max_size
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    role = "compute"
  }

  taint {
    key    = "workload"
    value  = "simulation"
    effect = "PREFER_NO_SCHEDULE"
  }

  tags = var.tags
}

################################################################################
# OIDC Provider (for IRSA)
################################################################################

data "tls_certificate" "eks" {
  url = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.main.identity[0].oidc[0].issuer

  tags = var.tags
}
