variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet ids for the DB subnet group."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security groups attached to the DB instance."
}

variable "instance_class" {
  type        = string
  description = "RDS instance class."
}

variable "allocated_storage" {
  type        = number
  description = "Allocated storage (GiB)."
}

variable "max_allocated_storage" {
  type        = number
  description = "Storage autoscaling ceiling (GiB)."
}

variable "engine_version" {
  type        = string
  description = "Postgres engine version."
}

variable "multi_az" {
  type        = bool
  description = "Run the instance multi-AZ."
}

variable "backup_retention" {
  type        = number
  description = "Automated backup retention in days (also enables PITR)."
}

variable "db_name" {
  type        = string
  description = "Initial database name."
}

variable "db_username" {
  type        = string
  description = "Master username."
}

variable "identifier" {
  type        = string
  description = "DB instance identifier."
  default     = "relay"
}

variable "parameter_group_family" {
  type        = string
  description = "Parameter group family matching the engine version."
  default     = "postgres16"
}
