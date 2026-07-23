variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC."
}

variable "azs" {
  type        = list(string)
  description = "Availability zones to spread subnets across."
}
