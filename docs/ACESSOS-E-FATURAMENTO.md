# Acessos, custos e faturamento por empresa

## O problema
Uma empresa pode ter contas espalhadas em várias Organizations / contas pagadoras, e cada
Organization pode ter contas de várias empresas:

```
Organização AB ── conta 100000000001 (empresa A)
               └─ conta 200000000001 (empresa B)
Organização AC ── conta 100000000002 (empresa A)
               └─ conta 900000000001 (compartilhada: dividida pela tag cost-center)
Organização AD ── conta 100000000003 (empresa A)
```

A empresa A precisa ver os custos de 100000000001, 100000000002, 100000000003 e a parte dela da
conta compartilhada — e nada da empresa B.

## Como funciona
1. **Custos entram por arquivo FOCUS** (o padrão FinOps que AWS, Azure e OCI exportam). Cada
   conta encontrada no arquivo é cadastrada automaticamente e ligada à Organization/pagadora.
2. **O administrador atribui cada conta a uma empresa** (tela *Contas de nuvem*):
   - conta inteira → todas as linhas da conta;
   - só linhas com uma tag (ex.: `cost-center = empresa-a`) → para contas compartilhadas;
   - com período de validade → para contas que mudaram de dono.
   Regras ambíguas (a mesma linha indo para duas empresas) são recusadas.
3. **Cada linha de custo recebe a empresa** no momento da importação ou quando a regra muda.
   O Postgres (Row-Level Security) só devolve linhas da empresa do usuário. Linhas sem regra ficam
   visíveis apenas ao administrador da plataforma.
4. Se uma conta for transferida de AB para AD, ela continua com a mesma empresa: a atribuição é
   pela conta, não pela Organization.

## Papéis
| Papel | Custos e relatórios | Invoices e NF | Recursos | Ações rápidas | Convidar pessoas |
|---|---|---|---|---|---|
| Administrador da empresa | ✅ | ✅ | ✅ | ✅ | ✅ (só da própria empresa) |
| Financeiro | ✅ | ✅ | — | — | — |
| Operador | ✅ | — | ✅ | ✅ | — |
| Leitura | ✅ | — | ✅ | — | — |

O administrador da plataforma (sua equipe) vê todas as empresas, importa custos, atribui contas,
publica documentos e administra usuários.

## Administração de usuários
- **Convidar**: informe o e-mail e o papel. Quem ainda não tem conta recebe um e-mail do
  Keycloak para definir a senha, confirmar o e-mail e cadastrar o MFA (link válido por 72 h).
- O administrador da empresa convida e remove pessoas **só da própria empresa**.
- O administrador da plataforma também pode **desativar** (efeito imediato: as sessões caem),
  **redefinir o MFA** (perdeu o celular), **encerrar sessões**, **reenviar convite** e tornar
  alguém administrador da plataforma.
- Toda alteração pede o código MFA de novo se o último tiver mais de 5 minutos, e vai para a
  auditoria.

## Invoices e notas fiscais
- Cada documento pertence a **uma** empresa. A fatura de uma Organization compartilhada nunca é
  exposta inteira a ninguém.
- O administrador publica PDF, XML da NF-e, ZIP ou CSV (até 20 MB), com competência, número,
  valor e vencimento. O tipo é conferido pelo conteúdo do arquivo; duplicados são recusados.
- Quem tem o papel Financeiro ou Administrador da empresa baixa os documentos. Cada download fica
  na auditoria.
- Para quem está numa Organization compartilhada, o **demonstrativo mensal** (CSV por conta e
  serviço) mostra exatamente o que é da empresa.
- Os arquivos ficam cifrados (AES-256-GCM) no volume `documents`, ou em um S3 compatível.

## Relatórios
- Período, agrupamento por **serviço, conta, região, mês, dia, nuvem ou qualquer tag**, filtro
  por conta e por tag, custo faturado ou efetivo (com descontos de Savings Plans/reservas).
- Exportação CSV (separador `;`, abre direto no Excel) com detalhe diário e tags.
- Moedas diferentes aparecem separadas, nunca somadas.

## Como exportar o FOCUS em cada nuvem
| Nuvem | Onde |
|---|---|
| AWS | Billing and Cost Management → Data Exports → *FOCUS 1.0 with AWS columns* (na conta pagadora) |
| Azure | Cost Management → Exports → *Cost and usage details (FOCUS)* |
| OCI | Billing → Cost and Usage Reports → *FOCUS* |

Na Fase 1 os workers buscam esses arquivos sozinhos; até lá, importe pela tela *Contas de nuvem*.
Um exemplo com o cenário acima está em `samples/focus-exemplo-aws.csv`.
