################################################################################
# ElastiCache Redis 7 — single node, volatile-lru
################################################################################

resource "aws_elasticache_subnet_group" "main" {
  name       = "${var.project}-${var.environment}"
  subnet_ids = var.private_data_subnet_ids

  tags = var.tags
}

resource "aws_security_group" "redis" {
  name_prefix = "${var.project}-redis-"
  vpc_id      = var.vpc_id
  description = "ElastiCache Redis security group"

  tags = merge(var.tags, {
    Name = "${var.project}-redis-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group_rule" "redis_ingress" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  source_security_group_id = var.eks_node_security_group_id
  security_group_id        = aws_security_group.redis.id
  description              = "Allow Redis from EKS nodes"
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${var.project}-${var.environment}"
  description          = "${var.project} Redis cluster"

  engine         = "redis"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = 6379

  num_cache_clusters = 1 # Single node for MVP cost — see RFC Section 7.4

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = var.auth_token

  parameter_group_name = aws_elasticache_parameter_group.main.name

  snapshot_retention_limit = 1
  snapshot_window          = "02:00-03:00"
  maintenance_window       = "Mon:03:00-Mon:04:00"

  automatic_failover_enabled = false # Single node

  tags = var.tags
}

resource "aws_elasticache_parameter_group" "main" {
  name   = "${var.project}-redis7-${var.environment}"
  family = "redis7"

  parameter {
    name  = "maxmemory-policy"
    value = "volatile-lru"
  }

  tags = var.tags
}
