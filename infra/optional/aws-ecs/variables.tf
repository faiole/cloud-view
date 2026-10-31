variable "env" {
  description = "Nome do ambiente (dev, staging, prod)"
  type        = string
}

variable "region" {
  description = "Região AWS da conta hub"
  type        = string
  default     = "sa-east-1"
}

variable "domain_name" {
  description = "Domínio do painel (ex.: painel.suaempresa.com.br). O Keycloak fica em auth.<domínio>."
  type        = string
}

variable "acm_certificate_arn" {
  description = "Certificado ACM (na mesma região) cobrindo o domínio e auth.<domínio>"
  type        = string
}

variable "route53_zone_id" {
  description = "Hosted zone para criar os registros DNS. Vazio = criar DNS manualmente."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "allowed_cidrs" {
  description = "Se preenchido, o WAF só aceita estes IPs (ex.: VPN/escritório). Vazio = qualquer IP."
  type        = list(string)
  default     = []
}

variable "image_tag" {
  description = "Tag das imagens no ECR (ex.: SHA do commit)"
  type        = string
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "web_desired_count" {
  type    = number
  default = 2
}

variable "keycloak_desired_count" {
  type    = number
  default = 2
}

variable "alarm_email" {
  description = "E-mail que recebe alarmes da própria plataforma"
  type        = string
  default     = ""
}
