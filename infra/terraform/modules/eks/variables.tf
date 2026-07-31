variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_app_subnet_ids" {
  type = list(string)
}

variable "cluster_version" {
  type    = string
  default = "1.31"
}

variable "cluster_role_arn" {
  type = string
}

variable "node_role_arn" {
  type = string
}

variable "general_instance_type" {
  type    = string
  default = "t4g.medium"
}

variable "general_desired_size" {
  type    = number
  default = 2
}

variable "general_min_size" {
  type    = number
  default = 2
}

variable "general_max_size" {
  type    = number
  default = 4
}

variable "compute_instance_type" {
  type    = string
  default = "c7g.large"
}

variable "compute_desired_size" {
  type    = number
  default = 0
}

variable "compute_min_size" {
  type    = number
  default = 0
}

variable "compute_max_size" {
  type    = number
  default = 6
}

variable "tags" {
  type    = map(string)
  default = {}
}
