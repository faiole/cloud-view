resource "aws_route53_record" "app" {
  for_each = var.route53_zone_id == "" ? toset([]) : toset([local.app_host, local.auth_host])
  zone_id  = var.route53_zone_id
  name     = each.value
  type     = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
