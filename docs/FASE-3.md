# Fase 3 — Azure

## O que foi entregue

| Item | Onde |
|---|---|
| **App hub multi-tenant**: cada cliente só dá consentimento e atribui roles; nenhum segredo por cliente. Certificado (client assertion RS256) ou client secret, cifrado no cofre | `app/azure/client.py`, tela *Conexões → Azure* |
| Alternativa: app próprio do cliente (por conexão) | `POST /api/admin/connections/azure` |
| Script de onboarding (`az` CLI): Reader, Cost Management Reader, papel opcional *Cloud View Operator*, leitura do export FOCUS | `onboarding/azure/` |
| Descoberta das subscriptions do tenant | `collectors/azure.py` |
| Inventário via **Resource Graph**: VMs (tamanho, vCPU, memória, SO, IPs, VNet/subnet, NSG), discos, IPs públicos, VNets, subnets, NSGs | idem |
| Achados: portas sensíveis abertas no NSG (da placa ou da subnet), VM exposta, disco e IP sem uso, IP SKU Basic, VM parada sem desalocar | idem |
| Saúde: **Resource Health** por VM e eventos do **Service Health** (incidentes e manutenções) | idem |
| Custos: **Cost Management Query** (faturado e amortizado) ou **export FOCUS no Blob Storage** (com tags; usa a execução mais recente) | idem |
| **Azure Advisor** (custo) como recomendações, com economia mensal | idem |
| Invoices (experimental) pela API de Billing, só para tenant de uma única empresa | idem |
| Ações em VM: reiniciar, ligar, desligar (**deallocate**, para de cobrar computação) | `collectors/azure.py`, `collectors/aws_actions.py` |
| Tipos de recurso equivalentes entre nuvens (instâncias/VMs, volumes/discos, IPs, redes) | `app/resource_types.py` |

Orçamentos, anomalias, alertas, notificações e recomendações por regra (Fase 2) passam a valer para
o Azure sem configuração extra.

## Como funciona a conta Azure no Cloud View
- Conexão = **tenant** (diretório) do cliente. Contas = **subscriptions** descobertas.
- A atribuição a empresas é igual à AWS: subscription inteira ou só os recursos/custos com uma tag.
- Custos via Cost Management não trazem tags; para dividir subscriptions compartilhadas por tag, use o
  export FOCUS em CSV.
- O Cost Management limita bastante as consultas; o coletor respeita o `Retry-After` e consulta o mês
  anterior só na primeira semana.

## Ações rápidas no Azure
O Azure não permite condicionar as permissões de ação à tag do recurso. Por isso:
1. atribua o papel *Cloud View Operator* só nos grupos de recursos onde ações podem acontecer;
2. o painel continua exigindo a tag `cloudview-actions=enabled` na VM, MFA recente e confirmação pelo nome.

*Desligar* usa **deallocate** (a VM para de cobrar computação; o IP público dinâmico é liberado).

## Testes
106 testes (SQLite e Postgres com RLS). O Azure é simulado por um servidor falso em memória
(`tests/fake_azure.py`) que responde às APIs de login, ARM, Resource Graph, Cost Management, Resource
Health, Advisor, Billing e Blob Storage. A assinatura do certificado (client assertion) é verificada.

## Ainda não verificado
- Contra um tenant Azure real.
- Formato das respostas de Billing (invoices) para cada tipo de contrato (MCA, EA, MOSP).
- Export FOCUS em Parquet não é lido; configure o export em CSV.

## Próximo: Fase 4
OCI: coleta automática (FOCUS reports, Search), Cloud Advisor, Monitoring e ações em instância.
