output "codedeploy_app_name" {
  value       = aws_codedeploy_app.app.name
  description = "CodeDeploy application name."
}

output "deployment_group_name" {
  value       = aws_codedeploy_deployment_group.app.deployment_group_name
  description = "CodeDeploy deployment group name."
}

output "deployment_config_name" {
  value       = aws_codedeploy_deployment_config.canary_5pct_15min.deployment_config_name
  description = "Canary 5% / 15 min deployment config name."
}
