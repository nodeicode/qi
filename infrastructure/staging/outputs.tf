output "instance_id" {
  value = module.staging.instance_id
}

output "log_group" {
  value = module.staging.log_group_name
}

output "monitor_log_group" {
  value = module.staging.monitor_log_group_name
}

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}
