# Fase 4 — OCI

## O que foi entregue

| Item | Onde |
|---|---|
| Cliente REST da OCI com **assinatura de requisição** (HTTP Signature rsa-sha256), sem SDK | `app/oci/client.py` |
| **Chave própria por conexão** (recomendado): o painel gera o par de chaves, mostra só a pública e o fingerprint; a privada vai para o cofre. Rotação com um clique | `POST /api/admin/connections/oci`, `…/oci-key` |
| **Identidade hub** opcional, com políticas cross-tenancy (*endorse/admit*) | `PUT /api/admin/oci/hub` |
| Políticas IAM prontas para copiar | `onboarding/oci/README.md`, tela *Conexões → OCI* |
| Descoberta de compartments (incluindo a raiz) e das regiões assinadas | `collectors/oci.py` |
| Inventário: instâncias (shape, OCPU/vCPU, memória, IPs, VCN/subnet, security lists e NSGs), block volumes, IPs reservados, VCNs, subnets | idem |
| Achados: portas sensíveis abertas (security list da subnet ou NSG da VNIC), instância exposta, volume e IP reservado sem uso, shape de geração antiga | idem |
| Saúde: manutenções agendadas das instâncias e **Announcements** da tenancy | idem |
| Custos: **Usage API** (diário por compartment, serviço e região) ou **FOCUS reports** que a Oracle gera para cada tenancy (com tags) | idem |
| **Cloud Advisor** como recomendações, com economia mensal | idem |
| Ações: reiniciar (SOFTRESET), ligar, desligar (SOFTSTOP) | `collectors/oci.py`, `collectors/aws_actions.py` |
| Correção: o worker real não carregava o app hub do Azure no contexto (só os testes montavam). Agora carrega AWS, Azure e OCI, com teste de regressão | `app/worker.py` |

Orçamentos, anomalias, alertas, notificações e recomendações por regra valem para a OCI sem
configuração extra.

## Modelo no painel
- Conexão = **tenancy** do cliente. Contas = **compartments** (cada um atribuído a uma empresa,
  inteiro ou por tag).
- Tags definidas aparecem como `namespace.chave` (ex.: `Operations.owner`), tags livres como estão.
- Ações exigem a tag `cloudview.actions=enabled` (definida, também imposta pela política da OCI) ou
  `cloudview-actions=enabled` (livre, imposta só pelo painel).

## Limitações conhecidas
- A OCI não expõe *status check* como a AWS: a saúde por instância vem das manutenções agendadas e
  dos Announcements. Instâncias sem evento aparecem com saúde "—".
- **Invoices**: não há API única para todos os tipos de contrato; publique as faturas pela tela.
- Nem todos os serviços aceitam acesso entre tenancies; se algum job falhar com
  `NotAuthorizedOrNotFound` no modo hub, use a chave própria.

## Testes
119 testes (SQLite e Postgres com RLS). A OCI é simulada por um servidor falso em memória
(`tests/fake_oci.py`) que **confere a assinatura de cada requisição** com a chave pública cadastrada
e responde às APIs de Identity, Compute, Networking, Block Storage, Announcements, Usage API,
Optimizer e Object Storage.

## Ainda não verificado
- Contra uma tenancy OCI real.
- Caminho exato dos FOCUS reports no bucket da Oracle (implementado como
  `FOCUS Reports/AAAA/MM/DD/*.csv.gz`).

## Próximo: Fase 5
Evolução: topologia de rede, relatório mensal em PDF, agendamento liga/desliga.
