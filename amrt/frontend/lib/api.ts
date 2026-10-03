// Thin client for the AMRT API. Same-origin only; the session cookie is HttpOnly and every
// state-changing call echoes the CSRF token returned at login.

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public detail: Record<string, unknown> = {}) {
    super(message);
  }
}

let csrf = "";
export function setCsrf(token: string | null | undefined) {
  csrf = token ?? "";
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-AMRT-CSRF"] = csrf;
  const res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
  const text = await res.text();
  let data: unknown = text;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    /* plain text response (metrics) */
  }
  if (!res.ok) {
    const d = (data ?? {}) as { error?: string; message?: string; detail?: Record<string, unknown> };
    throw new ApiError(res.status, d.error ?? `HTTP_${res.status}`, d.message ?? res.statusText, d.detail ?? {});
  }
  return data as T;
}

export const api = {
  get: <T,>(path: string) => call<T>("GET", path),
  post: <T,>(path: string, body: unknown = {}) => call<T>("POST", path, body),
};

export function fmt(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toLocaleString("en-IN", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function inr(n: number | null | undefined): string {
  if (n === null || n === undefined) return "DATA UNAVAILABLE";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

export function ts(t: number | null | undefined): string {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false });
}

export function age(t: number | null | undefined): string {
  if (!t) return "—";
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? `${s.toFixed(0)}s ago` : s < 3600 ? `${(s / 60).toFixed(0)}m ago` : `${(s / 3600).toFixed(1)}h ago`;
}
