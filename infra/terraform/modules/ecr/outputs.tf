output "repository_urls" {
  value = { for k, v in aws_ecr_repository.repos : k => v.repository_url }
}

output "registry_id" {
  value = values(aws_ecr_repository.repos)[0].registry_id
}
