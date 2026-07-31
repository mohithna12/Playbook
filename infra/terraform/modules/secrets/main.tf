################################################################################
# Secrets Manager — placeholder entries, values set manually or via rotation
################################################################################

resource "aws_secretsmanager_secret" "db_password" {
  name = "${var.project}/${var.environment}/db-password"
  tags = var.tags
}

resource "aws_secretsmanager_secret" "redis_auth_token" {
  name = "${var.project}/${var.environment}/redis-auth-token"
  tags = var.tags
}

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name = "${var.project}/${var.environment}/anthropic-api-key"
  tags = var.tags
}

resource "aws_secretsmanager_secret" "odds_api_key" {
  name = "${var.project}/${var.environment}/odds-api-key"
  tags = var.tags
}

resource "aws_secretsmanager_secret" "clerk_secret_key" {
  name = "${var.project}/${var.environment}/clerk-secret-key"
  tags = var.tags
}
