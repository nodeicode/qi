output "instance_id" {
  description = "EC2 instance ID — used by GitHub Actions SSM Run Command."
  value       = aws_instance.trader.id
}

output "instance_arn" {
  description = "EC2 instance ARN."
  value       = aws_instance.trader.arn
}

output "iam_role_arn" {
  description = "IAM role ARN attached to the instance — grant this role any extra permissions (S3, etc.)."
  value       = aws_iam_role.trader.arn
}

output "log_group_name" {
  description = "CloudWatch Log Group name for the trader service."
  value       = aws_cloudwatch_log_group.trader.name
}

output "monitor_log_group_name" {
  description = "CloudWatch Log Group name for monitor_metrics.jsonl."
  value       = aws_cloudwatch_log_group.monitor.name
}

output "security_group_id" {
  description = "Security group ID."
  value       = aws_security_group.trader.id
}
