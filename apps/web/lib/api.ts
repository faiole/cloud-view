"use client";

export type Me = {
  id: string;
  email: string;
  name: string | null;
  is_admin: boolean;
  tenants: Record<string, string>;
  mfa_age_seconds: number;
  step_up_valid: boolean;
};

export type Tenant = { id: string; name: string; slug: string; aws_external_id: string; created_at: string };
export type Member = { user_id: string; email: string; name: string | null; role: "operator" | "viewer" | "client" };
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
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
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

export const roleLabel: Record<string, string> = {
  operator: "Operador",
  viewer: "Leitura",
  client: "Portal do cliente",
};
