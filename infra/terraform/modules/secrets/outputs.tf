output "db_password_secret_arn" {
  value = aws_secretsmanager_secret.db_password.arn
}

output "redis_auth_token_secret_arn" {
  value = aws_secretsmanager_secret.redis_auth_token.arn
}
