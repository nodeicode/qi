output "instance_id" {
  value = module.prod.instance_id
}

output "log_group" {
  value = module.prod.log_group_name
}

output "monitor_log_group" {
  value = module.prod.monitor_log_group_name
}

output "github_actions_role_arn" {
  value = data.aws_iam_role.github_actions.arn
}
