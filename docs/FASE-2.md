# Fase 2 — Otimização, orçamentos, anomalias e alertas

## O que foi entregue

| Item | Onde |
|---|---|
| Recomendações do **AWS Cost Optimization Hub** (rightsizing, ociosos, Graviton, Savings Plans, reservas) | `collectors/aws_optimization.py`, aba *Otimização* |
| Recomendações próprias com economia estimada: volume sem uso, gp2 → gp3, IP elástico sem uso, geração antiga | `services/rule_recommendations.py` |
| Ciclo da recomendação: aberta → feita / dispensada; resolvida sozinha quando some da fonte | idem |
| **Cobertura de Savings Plans** por conta e mês | aba *Otimização* |
| **Orçamentos** mensais por empresa, com filtro por conta, serviço ou tag; avisos por % e por previsão | `services/budgets.py`, aba *Orçamentos* |
| **Anomalias de custo** próprias (qualquer nuvem) + as do AWS Cost Anomaly Detection | `services/anomalies.py` |
| **Alertas**: orçamento, anomalia, saúde, instância exposta, atualizações críticas, coleta com erro | `services/evaluate.py`, aba *Alertas*, tela *Alertas* (admin) |
| **Notificações**: e-mail, Slack, Teams (Adaptive Card) e webhook assinado (HMAC-SHA256) | `services/notify.py` |
| **Atualizações (SSM Patch Manager)**: pendências críticas/segurança por instância | `collectors/aws_inventory.py`, aba *Saúde* |
| Correção: o escopo de RLS agora é reaplicado a cada transação da mesma sessão | `core/db.py` |

## Como funciona

### Recomendações
- Cost Optimization Hub: habilite em *Billing and Cost Management → Cost Optimization Hub* na conta
  management (vale para a Organization toda). Sem ele, o job mostra o motivo e as regras próprias
  continuam funcionando.
- A empresa dona da recomendação segue as regras de atribuição (conta inteira ou tag do recurso).
- Valores com `*` são estimativas por preço de tabela (us-east-1, sob demanda).

### Orçamentos
- Realizado = soma do mês com os filtros do orçamento, na moeda do orçamento.
- Previsão = realizado + média diária dos últimos 7 dias com dado × dias restantes do mês.
- Cada limite (ex.: 50/80/100%) avisa **uma vez por mês**; a previsão acima do valor avisa uma vez.
- Quem cria e edita: Administrador da empresa e Financeiro.

### Anomalias
Para cada um dos últimos 3 dias com dado, compara o custo com a média e o desvio-padrão dos 14 dias
anteriores. É anomalia quando passa de média + max(3σ, 25% da média) e sobe pelo menos 20 na moeda.
O alerta lista os serviços que mais subiram. Funciona com AWS, Azure e OCI (qualquer custo importado).

### Alertas e notificações
- Avaliação a cada 30 minutos e logo depois de cada coleta de custos.
- Alertas que deixam de valer (evento fechado, porta fechada, patch aplicado) se resolvem sozinhos;
  se voltarem, reabrem e notificam de novo.
- Quem vê: alertas de custo exigem acesso a custos; de saúde e segurança, acesso a recursos.
- Canais da empresa (configurados pelo Administrador da empresa) e canais da plataforma (sua
  equipe, que recebe também os alertas das empresas e os de coleta com erro).
- URLs de Slack, Teams e webhook ficam cifradas no cofre. Slack só em `hooks.slack.com`, Teams só em
  domínios da Microsoft, webhook só HTTPS para endereços públicos.
- Webhook: `X-CloudView-Timestamp` e `X-CloudView-Signature: sha256=HMAC(segredo, "timestamp.corpo")`.
- Se todos os canais falharem, o envio é repetido até 3 vezes com espera crescente.

## Testes
93 testes (SQLite e Postgres com RLS). Cost Optimization Hub, Cost Explorer (cobertura e
anomalias), SSM e os destinos de notificação usam respostas falsas no formato real.

## Ainda não verificado
- Contra uma conta AWS real (Cost Optimization Hub, anomalias e SSM só simulados).
- Entrega real em Slack/Teams/SMTP (testada com o envio interceptado).

## Próximo: Fase 3
Azure: coleta automática (Cost Management, Resource Graph), Advisor, Resource Health e ações em VM.
