# Onboarding Azure

O Cloud View acessa o Azure do cliente com um **app multi-tenant** registrado no tenant da sua
empresa (o "app hub"). O cliente não precisa criar app nem enviar segredo: ele dá consentimento e
atribui as roles.

## 1. Uma vez, no seu tenant (plataforma)
1. *Entra ID → Registros de aplicativo → Novo registro*: tipo **Contas em qualquer diretório
   organizacional (multilocatário)**.
2. Em *Certificados e segredos*, envie um **certificado** (recomendado) ou crie um client secret.
3. No painel, tela **Conexões → App hub do Azure**, informe o client id, o tenant do app e o
   certificado + chave privada (ou o secret). Tudo vai cifrado para o cofre.

Gerar um certificado para o app:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 730 -subj "/CN=cloudview-hub" -keyout cloudview-hub.key -out cloudview-hub.crt
```

Envie `cloudview-hub.crt` no Azure e cole `.crt` e `.key` no painel.

## 2. Para cada cliente
1. No painel, **Conexões → Nova conexão Azure** com o ID do tenant do cliente. O painel mostra o
   link de consentimento:
   `https://login.microsoftonline.com/<tenant-do-cliente>/adminconsent?client_id=<app>`
2. Um administrador global do cliente abre o link e aceita.
3. No Cloud Shell do cliente:

```bash
./grant-access.sh <client-id-do-app> /providers/Microsoft.Management/managementGroups/<id>
```

Opções:
- `--actions`: cria e atribui o papel **Cloud View Operator** (reiniciar, ligar, desligar VMs).
  No Azure as permissões não podem ser condicionadas à tag; atribua o papel só nos grupos de
  recursos desejados. O painel ainda exige a tag `cloudview-actions=enabled` na VM.
- `--export-storage <id da conta de armazenamento>`: leitura do export FOCUS (custos com tags).

4. Volte ao painel e clique em **Testar**: as subscriptions aparecem em *Contas de nuvem*.

## Custos com tags (opcional)
Em *Cost Management → Exports*, crie um export **FOCUS (Cost and usage details)**, formato **CSV**,
diário, para um container. Informe na conexão o local
`https://<conta>.blob.core.windows.net/<container>/<pasta>`.

## Invoices (opcional, experimental)
Informe a **conta de cobrança** na conexão e dê ao app o papel *Billing account reader* nela.
Como na AWS, a invoice só é publicada se todos os custos do tenant no mês forem de uma única empresa.
