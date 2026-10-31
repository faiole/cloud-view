"use client";

export type Role = "tenant_admin" | "operator" | "finance" | "viewer";
export type Perm =
  | "resources.read"
  | "costs.read"
  | "reports.read"
  | "invoices.read"
  | "actions.execute"
  | "members.manage";

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
  tenant_admin: ["resources.read", "costs.read", "reports.read", "invoices.read", "actions.execute", "members.manage"],
  operator: ["resources.read", "costs.read", "reports.read", "actions.execute"],
  finance: ["costs.read", "reports.read", "invoices.read"],
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
