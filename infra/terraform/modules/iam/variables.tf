variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "oidc_provider_arn" {
  type    = string
  default = ""
}

variable "oidc_provider_url" {
  type    = string
  default = ""
}

variable "namespace" {
  type    = string
  default = "fantasyai"
}

variable "s3_bucket_arns" {
  type    = list(string)
  default = []
}

variable "tags" {
  type    = map(string)
  default = {}
}
