# Infra opcional

O caminho padrão do Cloud View é o `docker-compose.yml` da raiz: roda igual numa máquina local e
numa VM de qualquer nuvem (veja `docs/DEPLOY.md`).

`aws-ecs/` é uma alternativa para quem quiser rodar na AWS com serviços gerenciados
(ECS Fargate, RDS, ElastiCache, ALB + WAF). Não é necessária. Ao usá-la, rode a task de migração
com `DB_MANAGE_ROLES=true` (o usuário master do RDS cria os demais usuários).
