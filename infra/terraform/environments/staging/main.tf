################################################################################
# Staging Environment
################################################################################

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

locals {
  tags = {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# --- Networking ---

module "vpc" {
  source = "../../modules/vpc"

  project  = var.project
  vpc_cidr = var.vpc_cidr
  tags     = local.tags
}

# --- IAM (must come before EKS) ---

module "iam" {
  source = "../../modules/iam"

  project           = var.project
  environment       = var.environment
  aws_region        = var.aws_region
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  s3_bucket_arns    = values(module.s3.bucket_arns)
  tags              = local.tags
}

# --- EKS ---

module "eks" {
  source = "../../modules/eks"

  project                = var.project
  environment            = var.environment
  vpc_id                 = module.vpc.vpc_id
  private_app_subnet_ids = module.vpc.private_app_subnet_ids
  cluster_role_arn       = module.iam.eks_cluster_role_arn
  node_role_arn          = module.iam.eks_node_role_arn

  general_desired_size = 1 # Staging: single node
  general_min_size     = 1
  general_max_size     = 2
  compute_desired_size = 0
  compute_min_size     = 0
  compute_max_size     = 1

  tags = local.tags
}

# --- Database ---

module "rds" {
  source = "../../modules/rds"

  project                    = var.project
  environment                = var.environment
  vpc_id                     = module.vpc.vpc_id
  private_data_subnet_ids    = module.vpc.private_data_subnet_ids
  eks_node_security_group_id = module.eks.cluster_security_group_id
  instance_class             = "db.t4g.micro" # Staging: smaller
  allocated_storage          = 20
  max_allocated_storage      = 50
  master_password            = var.db_password
  monitoring_role_arn        = module.iam.rds_monitoring_role_arn
  tags                       = local.tags
}

# --- Redis ---

module "elasticache" {
  source = "../../modules/elasticache"

  project                    = var.project
  environment                = var.environment
  vpc_id                     = module.vpc.vpc_id
  private_data_subnet_ids    = module.vpc.private_data_subnet_ids
  eks_node_security_group_id = module.eks.cluster_security_group_id
  node_type                  = "cache.t4g.micro" # Staging: smaller
  auth_token                 = var.redis_auth_token
  tags                       = local.tags
}

# --- S3 ---

module "s3" {
  source = "../../modules/s3"

  project     = var.project
  environment = var.environment
  tags        = local.tags
}

# --- ALB ---

module "alb" {
  source = "../../modules/alb"

  project           = var.project
  environment       = var.environment
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnet_ids
  certificate_arn   = "" # No TLS in staging — HTTP only
  tags              = local.tags
}

# --- ECR ---

module "ecr" {
  source = "../../modules/ecr"

  project = var.project
  tags    = local.tags
}

# --- Secrets ---

module "secrets" {
  source = "../../modules/secrets"

  project     = var.project
  environment = var.environment
  tags        = local.tags
}
