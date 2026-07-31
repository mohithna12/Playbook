variable "project" {
  type = string
}

variable "environment" {
  type = string
}

variable "bucket_names" {
  type    = list(string)
  default = ["raw", "models", "features"]
}

variable "tags" {
  type    = map(string)
  default = {}
}
