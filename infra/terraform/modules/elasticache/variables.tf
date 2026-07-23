variable "environment" {
  type        = string
  description = "Deployment environment name."
}

variable "name" {
  type        = string
  description = "Logical name for this Redis cluster (e.g. cache | broker)."
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnet ids for the cache subnet group."
}

variable "security_group_ids" {
  type        = list(string)
  description = "Security groups attached to the replication group."
}

variable "node_type" {
  type        = string
  description = "ElastiCache node type."
}

variable "engine_version" {
  type        = string
  description = "Redis engine version."
}

variable "replica_count" {
  type        = number
  description = "Number of replicas per node group (0 = primary only)."
  default     = 1
}

variable "parameter_group_family" {
  type        = string
  description = "Redis parameter group family."
  default     = "redis7"
}

variable "maxmemory_policy" {
  type        = string
  description = "Eviction policy. 'allkeys-lru' suits a pure cache; 'noeviction' suits a broker that must not drop enqueued work."
  default     = "noeviction"
}

variable "appendonly" {
  type        = bool
  description = "Enable AOF persistence (true for the broker, false for the pure cache)."
  default     = false
}
