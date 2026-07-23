# ==============================================================================
# Relay production infrastructure — root composition.
# Productionizes infra/docker-compose.yml onto AWS (RFC-001 §6.1, §9, §13;
# RFC-002 §9). Four runtime shapes: app + workers + beat on ECS Fargate,
# gateway (Centrifugo) as its own Fargate service behind an NLB. web is on
# Vercel (off-platform, not modelled here).
# ==============================================================================

module "networking" {
  source = "./modules/networking"

  environment = var.environment
  vpc_cidr    = var.vpc_cidr
  azs         = var.azs
}

module "secrets" {
  source = "./modules/secrets"

  environment = var.environment
}

module "rds" {
  source = "./modules/rds"

  environment           = var.environment
  subnet_ids            = module.networking.private_subnet_ids
  security_group_ids    = [module.networking.rds_sg_id]
  instance_class        = var.rds_instance_class
  allocated_storage     = var.rds_allocated_storage
  max_allocated_storage = var.rds_max_allocated_storage
  engine_version        = var.rds_engine_version
  multi_az              = var.multi_az
  backup_retention      = var.backup_retention
  db_name               = var.db_name
  db_username           = var.db_username
}

# ElastiCache Redis x2 — cache/pubsub (also Centrifugo's engine) + Celery broker.
module "redis_cache" {
  source = "./modules/elasticache"

  environment        = var.environment
  name               = "cache"
  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = [module.networking.redis_sg_id]
  node_type          = var.redis_node_type
  engine_version     = var.redis_engine_version
  replica_count      = var.redis_cache_replicas
  maxmemory_policy   = "allkeys-lru"
  appendonly         = false
}

module "redis_broker" {
  source = "./modules/elasticache"

  environment        = var.environment
  name               = "broker"
  subnet_ids         = module.networking.private_subnet_ids
  security_group_ids = [module.networking.redis_sg_id]
  node_type          = var.redis_node_type
  engine_version     = var.redis_engine_version
  replica_count      = var.redis_broker_replicas
  maxmemory_policy   = "noeviction"
  appendonly         = true
}

# Gateway first: the app tier needs its internal API URL.
module "gateway" {
  source = "./modules/gateway"

  environment             = var.environment
  region                  = var.region
  vpc_id                  = module.networking.vpc_id
  private_subnet_ids      = module.networking.private_subnet_ids
  public_subnet_ids       = module.networking.public_subnet_ids
  gateway_sg_id           = module.networking.gateway_sg_id
  centrifugo_image        = var.gateway_image
  gateway_node_count      = var.gateway_node_count
  gateway_min_count       = var.gateway_min_count
  gateway_max_count       = var.gateway_max_count
  gateway_cpu             = var.gateway_cpu
  gateway_node_memory_mb  = var.gateway_memory_mb
  gateway_certificate_arn = var.gateway_certificate_arn
  redis_engine_address    = module.redis_cache.primary_endpoint
  centrifugo_token_arn    = module.secrets.centrifugo_token_arn
  centrifugo_apikey_arn   = module.secrets.centrifugo_apikey_arn
  secret_arns             = module.secrets.all_arns
}

module "ecs" {
  source = "./modules/ecs"

  environment        = var.environment
  region             = var.region
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  public_subnet_ids  = module.networking.public_subnet_ids
  app_sg_id          = module.networking.app_sg_id
  alb_sg_id          = module.networking.alb_sg_id

  image     = var.api_image
  image_tag = var.image_tag

  app_cpu              = var.app_cpu
  app_memory           = var.app_memory
  app_desired_count    = var.app_desired_count
  app_min_count        = var.app_min_count
  app_max_count        = var.app_max_count
  worker_cpu           = var.worker_cpu
  worker_memory        = var.worker_memory
  worker_desired_count = var.worker_desired_count
  beat_cpu             = var.beat_cpu
  beat_memory          = var.beat_memory

  database_endpoint     = module.rds.endpoint
  database_port         = module.rds.port
  db_name               = var.db_name
  redis_cache_endpoint  = module.redis_cache.primary_endpoint
  redis_broker_endpoint = module.redis_broker.primary_endpoint
  centrifugo_api_url    = module.gateway.internal_api_url

  jwt_signing_key_arn   = module.secrets.jwt_signing_key_arn
  centrifugo_token_arn  = module.secrets.centrifugo_token_arn
  centrifugo_apikey_arn = module.secrets.centrifugo_apikey_arn
  ses_credentials_arn   = module.secrets.ses_credentials_arn
  secret_arns           = module.secrets.all_arns
}

module "cdn" {
  source = "./modules/cdn"

  environment         = var.environment
  widget_bucket_name  = var.widget_bucket_name
  domain_names        = var.cdn_domain_names
  acm_certificate_arn = var.cdn_acm_certificate_arn
}

module "unleash" {
  source = "./modules/unleash"

  environment        = var.environment
  region             = var.region
  vpc_id             = module.networking.vpc_id
  private_subnet_ids = module.networking.private_subnet_ids
  alb_sg_id          = module.networking.unleash_alb_sg_id
  task_sg_id         = module.networking.unleash_task_sg_id
  db_sg_id           = module.networking.unleash_db_sg_id
  image              = var.unleash_image
  cpu                = var.unleash_cpu
  memory             = var.unleash_memory
  db_instance_class  = var.unleash_db_instance_class
  ecs_cluster_arn    = module.ecs.cluster_arn
}

module "observability" {
  source = "./modules/observability"

  environment                    = var.environment
  region                         = var.region
  alb_arn_suffix                 = module.ecs.alb_arn_suffix
  target_group_arn_suffix        = module.ecs.target_group_blue_arn_suffix
  ecs_cluster_name               = module.ecs.cluster_name
  app_service_name               = module.ecs.app_service_name
  alarm_5xx_threshold            = var.alarm_5xx_threshold
  alarm_p95_latency_seconds      = var.alarm_p95_latency_seconds
  alarm_queue_oldest_age_seconds = var.alarm_queue_oldest_age_seconds
  alert_email                    = var.alert_email
}

module "deploy" {
  source = "./modules/deploy"

  environment               = var.environment
  ecs_cluster_name          = module.ecs.cluster_name
  app_service_name          = module.ecs.app_service_name
  prod_listener_arn         = module.ecs.alb_listener_arn
  target_group_blue_name    = module.ecs.target_group_blue_name
  target_group_green_name   = module.ecs.target_group_green_name
  auto_rollback_alarm_names = module.observability.slo_burn_alarm_names
}
