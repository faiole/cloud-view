# Fase 5 — Topologia, relatório mensal e agenda liga/desliga

## O que foi entregue

| Item | Onde |
|---|---|
| **Topologia de rede** por empresa (AWS, Azure e OCI): VPC/VNet/VCN, subnets (públicas destacadas), instâncias com estado e IPs, ligação com a internet por subnet, instâncias expostas em vermelho; clique mostra IPs, regras e o motivo da exposição | aba *Topologia*, `GET /api/tenants/{id}/topology` |
| **Relatório mensal em PDF**: total e variação, tendência de 6 meses, por conta, serviço, tag e região, orçamentos, recomendações com economia, alertas do mês e situação das instâncias | `services/monthly_report.py`, botão em *Relatórios* |
| **Geração automática** no dia 3 de cada mês (mês anterior): fica em *Documentos* como "Relatório mensal" e vai em anexo para os canais de e-mail da empresa que recebem alertas de custo | `services/monthly_jobs.py` |
| **Agenda liga/desliga**: horários por dia da semana, no fuso escolhido, por tag (ex.: `agenda=comercial`) ou lista de instâncias, com horas desligadas por semana | aba *Agenda*, `services/power_schedules.py` |
| Quem vê custos (Leitura, Operador) passa a ver os relatórios mensais em *Documentos*; faturas e notas continuam restritas ao Financeiro e ao Administrador da empresa | `routers/documents.py` |

## Agenda liga/desliga
- Só age em instâncias com ações rápidas habilitadas (`cloudview-actions=enabled`, ou a tag definida
  `cloudview.actions=enabled` na OCI). A mesma proteção das ações manuais.
- Cada horário dispara **uma vez por dia**, dentro de uma janela de 15 minutos (tolera atraso do worker).
- Liga só o que está desligado e desliga só o que está ligado; se já houver uma ação em andamento
  para a instância, ela é pulada.
- Usa o mesmo caminho das ações manuais (fila `action`, `worker-actions`), aparece em *Ações recentes*
  como "agenda: <nome>" e fica na auditoria.
- Quem cria e edita: Operador e Administrador da empresa, com MFA recente.
- Exemplo: dias úteis das 08:00 às 20:00 = 108 h desligadas por semana (64% menos horas de computação).

## Relatório mensal
- Valores faturados no mês, como reportados pelas nuvens. Gerado no dia 3 para dar tempo de as
  nuvens fecharem os números; o botão permite gerar qualquer mês a qualquer momento.
- Se a empresa tiver mais de uma moeda, cada uma aparece separada.
- O PDF é gerado no servidor com `reportlab` (Python puro, sem dependências de sistema).

## Testes
127 testes (SQLite e Postgres com RLS). A agenda é testada com horário de São Paulo contra UTC,
repetição no mesmo dia, fim de semana e instâncias sem a tag. O relatório é testado nos números e na
geração do PDF; o job mensal, no arquivamento, no envio e em não duplicar.
