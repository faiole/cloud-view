# Onboarding OCI

Dois modos. **Chave própria** é o recomendado: funciona com todos os serviços e não depende de
políticas entre tenancies.

## Modo 1 — chave própria (recomendado)
1. No painel, **Conexões → OCI → Nova conexão** com o OCID da tenancy e a região home do cliente.
   O painel gera a chave de API e mostra **só a chave pública** e o fingerprint. A chave privada vai
   direto para o cofre e nunca é exibida.
2. Na tenancy do cliente (console OCI → *Identity & Security*):
   - crie o usuário `cloudview` (sem senha nem console) e o grupo `CloudViewReaders`, e coloque o usuário no grupo;
   - em *API Keys* do usuário, **cole a chave pública** mostrada pelo painel;
   - crie as políticas abaixo na compartment raiz.
3. Volte ao painel, informe o **OCID do usuário** na conexão e clique em **Testar**.

### Políticas (compartment raiz)

```
Allow group CloudViewReaders to inspect all-resources in tenancy
Allow group CloudViewReaders to read all-resources in tenancy
Allow group CloudViewReaders to read usage-report in tenancy
Allow group CloudViewReaders to read optimizer-api-family in tenancy
Allow group CloudViewReaders to read announcements in tenancy
```

Custos pelos FOCUS reports (com tags), além da Usage API:

```
Define tenancy usage-report as ocid1.tenancy.oc1..aaaaaaaaned4fkpkisbwjlr56u7cj63lf3wffbilvqknstgtvzub7vhqkggq
Endorse group CloudViewReaders to read objects in tenancy usage-report
```

Ações rápidas (reiniciar, ligar, desligar), só nas instâncias marcadas. A OCI permite condicionar a
política a uma **tag definida**: crie o namespace `cloudview` com a chave `actions` e marque as
instâncias com `cloudview.actions = enabled`.

```
Allow group CloudViewReaders to use instances in tenancy where target.resource.tag.cloudview.actions = 'enabled'
```

O painel aceita tanto a tag definida `cloudview.actions=enabled` quanto a tag livre
`cloudview-actions=enabled`; só a definida é imposta também pela política da OCI.

Rotação: **Gerar nova chave** na conexão, adicione a nova chave pública ao usuário, aguarde a
próxima coleta e remova a antiga.

## Modo 2 — identidade hub (cross-tenancy)
Um usuário na tenancy da sua empresa acessa as tenancies dos clientes por políticas *endorse/admit*.
Em *Conexões → OCI → Identidade hub*, informe a tenancy e o usuário; o painel gera a chave e mostra a
pública para colar no usuário.

Na **sua** tenancy:

```
Define tenancy Cliente as <ocid-da-tenancy-do-cliente>
Endorse group CloudViewHub to read all-resources in tenancy Cliente
Endorse group CloudViewHub to read usage-report in tenancy Cliente
```

Na tenancy do **cliente**:

```
Define tenancy Plataforma as <ocid-da-sua-tenancy>
Define group CloudViewHub as <ocid-do-grupo-na-sua-tenancy>
Admit group CloudViewHub of tenancy Plataforma to read all-resources in tenancy
Admit group CloudViewHub of tenancy Plataforma to read usage-report in tenancy
```

Nem todos os serviços da OCI aceitam acesso entre tenancies (Cloud Advisor e Announcements podem
recusar). Se algum job mostrar "NotAuthorizedOrNotFound", use o modo chave própria nessa conexão.

## O que cada compartment vira no painel
Cada compartment (e a raiz) aparece em *Contas de nuvem* e é atribuído a uma empresa, inteiro ou por
tag. Tags definidas aparecem como `namespace.chave`.

## Invoices
A OCI não tem uma API de invoices única para todos os tipos de contrato. Publique as faturas e notas
fiscais pela tela *Invoices e notas* do cliente.
