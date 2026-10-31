"use client";

export type Role = "tenant_admin" | "operator" | "finance" | "viewer";
export type Perm =
  | "resources.read"
  | "costs.read"
  | "reports.read"
  | "invoices.read"
  | "actions.execute"
  | "members.manage"
  | "finops.manage";

export type Me = {
  id: string;
  email: string;
  name: string | null;
  is_admin: boolean;
  tenants: Record<string, Role>;
  mfa_age_seconds: number;
  step_up_valid: boolean;
};

export type Tenant = { id: string; name: string; slug: string; aws_external_id: string; created_at: string };
export type Member = {
  user_id: string;
  email: string;
  name: string | null;
  role: Role;
  disabled: boolean;
  pending: boolean;
  invite_email_sent: boolean | null;
};
export type AuditEntry = {
  id: string;
  ts: string;
  user_email: string | null;
  tenant_id: string | null;
  ip: string | null;
  action: string;
  target: string | null;
  details: Record<string, unknown>;
  outcome: string;
};
export type Total = { currency: string; cost: string };
export type UsageRow = { key: string; label: string; currency: string; cost: string };
export type UsageReport = {
  start: string;
  end: string;
  group_by: string;
  totals: Total[];
  rows: UsageRow[];
  daily: { date: string; currency: string; cost: string }[];
};
export type CostSummary = {
  month_to_date: Total[];
  previous_month: Total[];
  monthly: UsageRow[];
  top_services: UsageRow[];
  accounts: number;
};
export type TenantAccount = { id: string; provider: string; native_id: string; name: string | null; shared: boolean; rules: string[] };
export type Doc = {
  id: string;
  kind: string;
  provider: string | null;
  billing_period: string;
  number: string | null;
  issuer: string | null;
  amount: string | null;
  currency: string | null;
  due_date: string | null;
  filename: string;
  content_type: string;
  size_bytes: number;
  created_at: string;
};
export type AdminUser = {
  id: string;
  email: string;
  name: string | null;
  disabled: boolean;
  pending: boolean;
  last_login_at: string | null;
  memberships: { tenant_id: string; tenant_name: string; role: Role }[];
  invite_email_sent: boolean | null;
};
export type CloudAccount = {
  id: string;
  provider: string;
  native_id: string;
  name: string | null;
  connection_name: string | null;
  connection_native_id: string | null;
  tenants: string[];
};
export type Assignment = {
  id: string;
  tenant_id: string;
  tag_key: string | null;
  tag_value: string | null;
  valid_from: string | null;
  valid_to: string | null;
};
export type ImportResult = {
  id: string;
  filename: string | null;
  period_start: string | null;
  period_end: string | null;
  source_rows: number;
  stored_rows: number;
  replaced_rows: number;
  created_by: string | null;
  created_at: string;
  new_accounts: string[];
  unassigned_accounts: string[];
};

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
}

function csrf(): string {
  const m = document.cookie.match(/(?:^|; )cv_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

function here(): string {
  return encodeURIComponent(window.location.pathname + window.location.search);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (typeof init.body === "string" && !headers.has("content-type")) headers.set("content-type", "application/json");
  if (method !== "GET") headers.set("x-csrf-token", csrf());

  const res = await fetch(`/api${path}`, { ...init, headers, credentials: "same-origin" });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);

  if (res.status === 401) {
    const detail = body?.detail;
    // Ação sensível sem MFA recente → reautenticar (senha + código) e voltar para a mesma tela
    if (detail?.error === "step_up_required") {
      window.location.href = `/api/auth/step-up?return_to=${here()}`;
    } else {
      window.location.href = `/api/auth/login?return_to=${here()}`;
    }
    throw new ApiError(401, detail);
  }
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? res.statusText);
  return body as T;
}

export function errorText(e: unknown, fallback = "Não foi possível concluir a operação."): string {
  if (e instanceof ApiError && typeof e.detail === "string" && !e.detail.includes("_")) return e.detail;
  if (e instanceof ApiError && e.status === 403) return "Você não tem permissão para isso.";
  return fallback;
}

export const roleLabel: Record<Role, string> = {
  tenant_admin: "Administrador da empresa",
  operator: "Operador",
  finance: "Financeiro",
  viewer: "Leitura",
};

export const roleHelp: Record<Role, string> = {
  tenant_admin: "Tudo da empresa, inclusive convidar pessoas",
  operator: "Recursos, custos, relatórios e ações rápidas",
  finance: "Custos, relatórios, invoices e notas fiscais",
  viewer: "Recursos, custos e relatórios",
};

const ROLE_PERMS: Record<Role, Perm[]> = {
  tenant_admin: ["resources.read", "costs.read", "reports.read", "invoices.read", "actions.execute", "members.manage", "finops.manage"],
  operator: ["resources.read", "costs.read", "reports.read", "actions.execute"],
  finance: ["costs.read", "reports.read", "invoices.read", "finops.manage"],
  viewer: ["resources.read", "costs.read", "reports.read"],
};

export function can(me: Me | null, tenantId: string, perm: Perm): boolean {
  if (!me) return false;
  if (me.is_admin) return true;
  const role = me.tenants[tenantId];
  return !!role && ROLE_PERMS[role].includes(perm);
}

export function money(value: string | number, currency = "USD"): string {
  try {
    return new Intl.NumberFormat("pt-BR", { style: "currency", currency }).format(Number(value));
  } catch {
    return `${currency} ${Number(value).toFixed(2)}`;
  }
}

export const kindLabel: Record<string, string> = {
  invoice: "Invoice",
  nota_fiscal: "Nota fiscal",
  boleto: "Boleto",
  statement: "Demonstrativo",
  other: "Outro",
};

// --- Fase 1 --------------------------------------------------------------------------------------

export type Finding = { code: string; severity: "critical" | "warning" | "info"; message: string };
export type Resource = {
  id: string;
  account_native_id: string;
  account_name: string | null;
  region: string;
  type: string;
  native_id: string;
  name: string | null;
  state: string | null;
  instance_type: string | null;
  vcpu: number | null;
  memory_mib: number | null;
  platform: string | null;
  private_ips: string[];
  public_ips: string[];
  vpc_id: string | null;
  subnet_id: string | null;
  security_groups: { id: string; name: string | null }[];
  tags: Record<string, string>;
  attributes: Record<string, unknown>;
  findings: Finding[];
  health: string | null;
  health_details: Record<string, string | null>;
  collected_at: string;
};
export type HealthEvent = {
  id: string;
  source: string;
  service: string | null;
  region: string | null;
  category: string | null;
  status: string;
  title: string;
  description: string | null;
  start_at: string | null;
  end_at: string | null;
};
export type HealthSummary = { instances: number; running: number; impaired: Resource[]; events: HealthEvent[]; last_collected_at: string | null };
export type NetworkSummary = {
  vpcs: { vpc: Resource; subnets: Resource[]; instances: number; exposed_instances: Resource[] }[];
  open_security_groups: Resource[];
  findings_by_code: Record<string, number>;
};
export type ActionReq = {
  id: string;
  resource_id: string;
  resource_native_id: string;
  action: string;
  reason: string | null;
  status: string;
  requested_by: string;
  requested_at: string;
  finished_at: string | null;
  result: Record<string, string>;
};
export type ConnectionDetail = {
  id: string;
  kind: string;
  native_id: string;
  name: string;
  role_arn: string | null;
  external_id: string | null;
  member_role_name: string;
  regions: string[] | null;
  cost_source: string;
  focus_s3_uri: string | null;
  collect_invoices: boolean;
  status: string;
  last_error: string | null;
  last_sync_at: string | null;
  accounts: number;
};
export type Job = {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  error: string | null;
  result: Record<string, unknown>;
  created_at: string;
  finished_at: string | null;
};
export type HubStatus = { mode: "default" | "vault"; principal_arn: string | null; access_key_hint: string | null; updated_at: string | null };

export const actionLabel: Record<string, string> = { reboot: "Reiniciar", start: "Ligar", stop: "Desligar" };
export const typeLabel: Record<string, string> = {
  ec2_instance: "Instâncias",
  ebs_volume: "Volumes",
  elastic_ip: "IPs elásticos",
  vpc: "VPCs",
  subnet: "Subnets",
  security_group: "Security groups",
};

export function ago(iso: string | null): string {
  if (!iso) return "nunca";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "agora";
  if (s < 3600) return `há ${Math.round(s / 60)} min`;
  if (s < 86400) return `há ${Math.round(s / 3600)} h`;
  return `há ${Math.round(s / 86400)} d`;
}

// --- Fase 2 --------------------------------------------------------------------------------------

export type Recommendation = {
  id: string;
  source: string;
  action_type: string;
  resource_type: string | null;
  resource_native_id: string | null;
  region: string | null;
  title: string;
  current: string | null;
  recommended: string | null;
  monthly_savings: string;
  monthly_cost: string | null;
  currency: string;
  savings_pct: string | null;
  effort: string | null;
  restart_needed: boolean | null;
  estimated: boolean;
  status: string;
  status_by: string | null;
  status_note: string | null;
  account_native_id: string | null;
};
export type RecommendationSummary = { open: number; savings: Total[]; by_action: Record<string, number>; items: Recommendation[] };
export type Coverage = { month: string; covered: string; on_demand: string; total: string; coverage_pct: string | null };
export type Budget = {
  id: string;
  name: string;
  amount: string;
  currency: string;
  accounts: string[];
  services: string[];
  tag: string | null;
  thresholds: number[];
  alert_on_forecast: boolean;
  actual: string | null;
  forecast: string | null;
  pct: string | null;
  last_evaluated_at: string | null;
};
export type Alert = {
  id: string;
  tenant_id: string | null;
  tenant_name: string | null;
  type: string;
  category: string;
  severity: "info" | "warning" | "critical";
  title: string;
  message: string;
  status: string;
  status_by: string | null;
  created_at: string;
  updated_at: string;
};
export type Channel = {
  id: string;
  type: string;
  name: string;
  emails: string[];
  target_hint: string | null;
  min_severity: string;
  categories: string[];
  enabled: boolean;
  signing_secret: string | null;
  last_delivery: string | null;
};

export const recActionLabel: Record<string, string> = {
  Rightsize: "Redimensionar",
  Stop: "Desligar ocioso",
  Upgrade: "Atualizar",
  PurchaseSavingsPlans: "Savings Plans",
  PurchaseReservedInstances: "Reservas",
  MigrateToGraviton: "Graviton",
  Delete: "Remover sem uso",
  ScaleIn: "Reduzir capacidade",
};
export const effortLabel: Record<string, string> = { VeryLow: "muito baixo", Low: "baixo", Medium: "médio", High: "alto", VeryHigh: "muito alto" };
export const severityLabel: Record<string, string> = { info: "informativo", warning: "atenção", critical: "crítico" };
export const categoryLabel: Record<string, string> = { cost: "Custos", health: "Saúde", security: "Segurança", platform: "Plataforma" };
