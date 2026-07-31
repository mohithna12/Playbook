variable "project" {
  type = string
}

variable "repository_names" {
  type    = list(string)
  default = ["api", "worker", "ml"]
}

variable "tags" {
  type    = map(string)
  default = {}
}
