################################################################################
# RDS PostgreSQL 16 — single-AZ for MVP cost constraint
################################################################################

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-${var.environment}"
  subnet_ids = var.private_data_subnet_ids

  tags = merge(var.tags, {
    Name = "${var.project}-db-subnet-group"
  })
}

resource "aws_security_group" "rds" {
  name_prefix = "${var.project}-rds-"
  vpc_id      = var.vpc_id
  description = "RDS PostgreSQL security group"

  tags = merge(var.tags, {
    Name = "${var.project}-rds-sg"
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_security_group_rule" "rds_ingress" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = var.eks_node_security_group_id
  security_group_id        = aws_security_group.rds.id
  description              = "Allow PostgreSQL from EKS nodes"
}

resource "aws_db_instance" "main" {
  identifier = "${var.project}-${var.environment}"

  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.database_name
  username = var.master_username
  password = var.master_password

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]

  multi_az            = false # Accepted risk for cost — see RFC Section 24
  publicly_accessible = false

  backup_retention_period = 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "Mon:04:00-Mon:05:00"

  deletion_protection       = var.environment == "prod"
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${var.project}-final-${var.environment}" : null

  performance_insights_enabled = true
  monitoring_interval          = 60
  monitoring_role_arn          = var.monitoring_role_arn

  parameter_group_name = aws_db_parameter_group.main.name

  tags = var.tags
}

resource "aws_db_parameter_group" "main" {
  name_prefix = "${var.project}-pg16-"
  family      = "postgres16"

  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # Log queries over 1 second
  }

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
  }

  tags = var.tags

  lifecycle {
    create_before_destroy = true
  }
}
