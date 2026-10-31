# Fase 1 — Coleta automática da AWS

## O que foi entregue

| Item | Onde |
|---|---|
| Conexões AWS (conta avulsa ou Organization), ExternalId por conexão, comandos de onboarding prontos | tela *Conexões*, `app/routers/connections.py` |
| Identidade hub: instance profile (modo padrão) ou chave IAM guardada no cofre | `app/aws/session.py` |
| Fila de jobs e agendador no Postgres (`FOR UPDATE SKIP LOCKED`), sem serviço de fila externo | `app/services/jobs.py`, `app/services/scheduler.py` |
| Workers separados: coleta e ações rápidas | `app/worker.py`, serviços `worker` e `worker-actions` |
| Descoberta das contas da Organization (contas novas entram sozinhas) | `collectors/aws_inventory.py` |
| Inventário: EC2 (modelo, vCPU, memória, SO, IPs, VPC, subnet, SG, tags), EBS, IPs elásticos | idem |
| Rede: VPCs, subnets, security groups; portas sensíveis abertas para a internet | idem, aba *Rede* |
| Dicas simples: volume/IP sem uso, gp2 → gp3, geração antiga, IMDSv1, disco sem criptografia | idem |
| Saúde: status checks, manutenções agendadas, AWS Health (visão organizacional) | idem, aba *Saúde* |
| Custos: Cost Explorer (sem tags) ou Data Export FOCUS no S3 (com tags) | `collectors/aws_billing.py` |
| Invoices em PDF publicadas automaticamente quando a pagadora é de uma única empresa | idem |
| Ações rápidas: reiniciar, ligar, desligar — MFA recente, confirmação pelo nome, tag obrigatória, auditoria | `routers/inventory.py`, `collectors/aws_actions.py` |

## Frequência da coleta

| Coleta | Intervalo | Observação |
|---|---|---|
| Descoberta de contas | 6 h | também no botão *Testar* |
| Inventário e rede | 15 min | por conta, todas as regiões habilitadas (ou as escolhidas) |
| Status checks | 5 min | só regiões com instâncias |
| AWS Health | 15 min | exige suporte Business/Enterprise; sem ele, o motivo aparece no job |
| Custos | 12 h | mês atual; mês anterior só na primeira semana (Cost Explorer cobra US$ 0,01/chamada) |
| Invoices | 24 h | últimos 3 meses |

## Regras de visibilidade
- Recursos e eventos de saúde seguem as mesmas regras de atribuição dos custos (conta inteira ou tag).
- Evento de saúde da conta (não de um recurso) só aparece se a conta for inteira de uma empresa.
- Invoice oficial só é publicada se **todos** os custos da pagadora no mês forem de uma única
  empresa. Em Organizations compartilhadas, a empresa usa o demonstrativo mensal.

## Ações rápidas
Para uma instância aceitar ações, três coisas precisam ser verdade:
1. a stack foi implantada com `EnableQuickActions=true` (cria a role `CloudViewOperator`);
2. a instância tem a tag `cloudview-actions=enabled` (a role só age com ela);
3. quem pede tem papel *Operador* ou *Administrador da empresa* e fez MFA há menos de 5 minutos.

A ação é confirmada digitando o nome da instância, vai para a fila do `worker-actions` e nunca é
repetida automaticamente em caso de erro.

## Testes
72 testes (SQLite e Postgres com RLS). A AWS é simulada com `moto` (EC2, Organizations, STS, S3);
Cost Explorer, Health e Invoicing usam respostas falsas no formato da API.

## Ainda não verificado
- Coleta contra uma conta AWS real (só simulador).
- Formato exato das respostas de `invoicing` e `health` em produção (implementados pelo modelo
  do boto3, testados com respostas falsas).
- Data Export FOCUS em Parquet não é lido; configure o export em CSV.

## Próximo: Fase 2
Otimização (Compute Optimizer, Cost Optimization Hub, Savings Plans), orçamentos, anomalias de
custo e notificações (e-mail, Teams, Slack).
