# Onboarding AWS

## Conta avulsa
1. No painel, abra o cliente e copie o **ExternalId**.
2. Na conta do cliente, crie a stack com `cloudformation/cloudview-roles.yaml`:

```bash
aws cloudformation deploy \
  --stack-name cloudview-access \
  --template-file onboarding/aws/cloudformation/cloudview-roles.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    CollectorPrincipalArn=<worker_collect_role_arn> \
    ActionsPrincipalArn=<worker_actions_role_arn> \
    ExternalId=<external_id_do_cliente> \
    EnableQuickActions=false
```

## AWS Organizations
Na conta management (ou delegated admin de StackSets), crie um **StackSet service-managed** com
auto-deployment habilitado apontando para a OU desejada. Contas novas recebem as roles sozinhas.
Faça também o deploy na própria conta management (StackSets não cobrem a conta management).

```bash
aws cloudformation create-stack-set \
  --stack-set-name cloudview-access \
  --template-body file://onboarding/aws/cloudformation/cloudview-roles.yaml \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=CollectorPrincipalArn,ParameterValue=<arn> \
               ParameterKey=ActionsPrincipalArn,ParameterValue=<arn> \
               ParameterKey=ExternalId,ParameterValue=<external_id> \
               ParameterKey=EnableQuickActions,ParameterValue=false
```

```bash
aws cloudformation create-stack-instances \
  --stack-set-name cloudview-access \
  --deployment-targets OrganizationalUnitIds=<ou-xxxx> \
  --regions us-east-1
```

As ações rápidas só funcionam em recursos com a tag `cloudview-actions=enabled`.
