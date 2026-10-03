"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, ApiError, setCsrf } from "@/lib/api";
import { Badge, DataLabelBadge } from "./ui";

type User = { username: string; role: string; csrf: string | null; step_up_age_s: number };
type AuthState = { setup_required: boolean; user: User | null; step_up_ttl_s: number };
type Status = {
  mode: { mode: string; banner: string; paper_auto_approve: boolean };
  environment: string; live_capable: boolean; blockers: string[]; simulated_market: boolean; data_source: string | null;
  readiness: { status: string; reason?: string; failed?: string[] }; accounts: { account_id: string; kind: string }[];
  safety: { kill_switch: { engaged: boolean; file_engaged: boolean; reason?: string }; emergency: { level: string } };
};
type Alert = { alert_id: string; severity: string; title: string; body: string; ts: number; audible?: boolean };

type Ctx = {
  user: User; status: Status | null; refreshStatus: () => void;
  act: <T = unknown>(path: string, body?: unknown, opts?: { confirm?: string }) => Promise<T | null>;
  lastError: string | null;
};
const ShellCtx = createContext<Ctx | null>(null);
export const useShell = () => {
  const c = useContext(ShellCtx);
  if (!c) throw new Error("outside shell");
  return c;
};

const NAV: [string, string][] = [
  ["/", "Overview"], ["/market/", "Option Chain & PCR"], ["/decisions/", "AI Decisions"], ["/approvals/", "Approvals"], ["/orders/", "Orders"],
  ["/portfolio/", "Portfolio"], ["/risk/", "Risk & Mode"], ["/fii-dii/", "FII / DII"], ["/backtest/", "Backtest"], ["/reliability/", "Reliability"],
  ["/brokers/", "Brokers"], ["/audit/", "Audit"], ["/settings/", "Settings"],
];

function beep() {
  try {
    const Ctor = (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext);
    const ctx = new Ctor();
    [0, 0.35, 0.7].forEach((t) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = "square"; o.frequency.value = 880; g.gain.value = 0.15;
      o.connect(g); g.connect(ctx.destination);
      o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.2);
    });
  } catch { /* audio blocked until the user interacts with the page */ }
}

function Login({ state, onDone }: { state: AuthState; onDone: () => void }) {
  const [u, setU] = useState(""), [p, setP] = useState(""), [t, setT] = useState(""), [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    try {
      if (state.setup_required) await api.post("/api/auth/setup", { code, username: u, password: p, totp_secret: t || null });
      const r = await api.post<{ csrf: string }>("/api/auth/login", { username: u, password: p, totp: state.setup_required ? null : t || null });
      setCsrf(r.csrf);
      onDone();
    } catch (x) {
      setErr(x instanceof ApiError ? `${x.code}: ${x.message}` : String(x));
    }
  };
  return (
    <div className="login">
      <form onSubmit={submit} className="card">
        <h1>AI Market Risk Terminal</h1>
        <p className="muted">{state.setup_required ? "First run: create the OWNER account with the one-time setup code printed in the server console." : "Sign in"}</p>
        {state.setup_required && <input placeholder="one-time setup code" value={code} onChange={(e) => setCode(e.target.value)} autoComplete="off" />}
        <input placeholder="username" value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" />
        <input placeholder="password (min 10 chars)" type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete={state.setup_required ? "new-password" : "current-password"} />
        <input placeholder={state.setup_required ? "TOTP secret (optional, base32)" : "TOTP code (if enabled)"} value={t} onChange={(e) => setT(e.target.value)} autoComplete="one-time-code" />
        <button type="submit" className="primary">{state.setup_required ? "Create owner & sign in" : "Sign in"}</button>
        {err && <div className="note err">{err}</div>}
      </form>
    </div>
  );
}

export default function Shell({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [auth, setAuth] = useState<AuthState | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [stepUp, setStepUp] = useState<{ resolve: (ok: boolean) => void } | null>(null);
  const [pw, setPw] = useState("");
  const [lastError, setLastError] = useState<string | null>(null);
  const [wsUp, setWsUp] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const loadAuth = useCallback(async () => {
    const a = await api.get<AuthState>("/api/auth/state");
    setCsrf(a.user?.csrf);
    setAuth(a);
  }, []);
  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.get<Status>("/api/status"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) loadAuth();
    }
  }, [loadAuth]);

  useEffect(() => { loadAuth().catch(() => setAuth({ setup_required: false, user: null, step_up_ttl_s: 300 })); }, [loadAuth]);
  useEffect(() => {
    if (!auth?.user) return;
    refreshStatus();
    const id = setInterval(refreshStatus, 3000);
    return () => clearInterval(id);
  }, [auth?.user, refreshStatus]);

  useEffect(() => {
    if (!auth?.user) return;
    let stop = false;
    const connect = () => {
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
      wsRef.current = ws;
      ws.onopen = () => setWsUp(true);
      ws.onclose = () => { setWsUp(false); if (!stop) setTimeout(connect, 3000); };
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        if (msg.topic === "alert") {
          const a = msg.payload as Alert;
          setAlerts((xs) => [a, ...xs].slice(0, 20));
          if (a.audible) beep();
        }
      };
    };
    connect();
    return () => { stop = true; wsRef.current?.close(); };
  }, [auth?.user]);

  const askStepUp = () => new Promise<boolean>((resolve) => setStepUp({ resolve }));

  const act = useCallback(async <T,>(p: string, body: unknown = {}, opts?: { confirm?: string }): Promise<T | null> => {
    if (opts?.confirm && !window.confirm(opts.confirm)) return null;
    setLastError(null);
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const r = await api.post<T>(p, body);
        refreshStatus();
        return r;
      } catch (e) {
        if (e instanceof ApiError && e.code === "STEP_UP_REQUIRED" && attempt === 0) {
          if (await askStepUp()) continue;
          setLastError("Step-up cancelled");
          return null;
        }
        const msg = e instanceof ApiError ? `${e.code}: ${e.message}${e.detail?.failed ? ` — failed: ${(e.detail.failed as string[]).join(", ")}` : ""}` : String(e);
        setLastError(msg);
        return null;
      }
    }
    return null;
  }, [refreshStatus]);

  if (!auth) return <div className="login"><div className="card">Loading…</div></div>;
  if (!auth.user) return <Login state={auth} onDone={loadAuth} />;

  const mode = status?.mode.mode ?? "PAPER";
  const killed = status?.safety.kill_switch.engaged || status?.safety.kill_switch.file_engaged;
  const doStepUp = async (ok: boolean) => {
    if (!stepUp) return;
    if (ok) {
      try { await api.post("/api/auth/step-up", { password: pw }); } catch (e) { setLastError(e instanceof ApiError ? e.message : String(e)); ok = false; }
    }
    setPw("");
    stepUp.resolve(ok);
    setStepUp(null);
  };

  return (
    <ShellCtx.Provider value={{ user: auth.user, status, refreshStatus, act, lastError }}>
      <div className={`mode-banner mode-${mode.toLowerCase()}`}>
        <strong>{status?.mode.banner ?? "PAPER MODE — NO REAL ORDERS"}</strong>
        <span>{status?.environment === "PAPER_ONLY" ? "PAPER_ONLY deployment — live order flow disabled" : "LIVE_CAPABLE deployment"}</span>
        {status?.simulated_market && <span className="badge b-purple">SIMULATED MARKET DATA</span>}
        <span>Readiness: <Badge v={status?.readiness.status ?? "NOT READY"} title={status?.readiness.reason ?? (status?.readiness.failed ?? []).join(", ")} /></span>
        <span className={wsUp ? "ok" : "warn"}>{wsUp ? "● live push" : "○ push disconnected"}</span>
      </div>
      <div className="topbar">
        <div className="blockers">
          {killed && <span className="badge b-red blink">KILL SWITCH ENGAGED</span>}
          {status?.safety.emergency.level !== "NONE" && status && <span className="badge b-red">EMERGENCY {status.safety.emergency.level}</span>}
          {(status?.blockers ?? []).filter((b) => b !== "KILL_SWITCH").map((b) => <span key={b} className="badge b-amber">{b}</span>)}
          {status && !status.blockers.length && <span className="badge b-green">no new-risk blockers</span>}
          <span className="muted">data: {status?.data_source ?? "none"}</span>
        </div>
        <div className="actions">
          <button className="danger" onClick={() => act("/api/safety/kill", { reason: "owner pressed KILL SWITCH" }, { confirm: "Engage the KILL SWITCH? All new risk stops immediately. Release needs step-up and a passing recovery verification." })}>KILL SWITCH</button>
          <button className="danger outline" onClick={() => {
            const acct = window.prompt(`Manual Quick Exit — account to flatten (${(status?.accounts ?? []).map((a) => a.account_id).join(", ")}):`, status?.accounts[0]?.account_id);
            if (!acct) return;
            const c = window.prompt("Type EXIT to send reduce-only exits for every open position on " + acct);
            if (c) act("/api/safety/quick-exit", { account_id: acct, confirmation: c });
          }}>MANUAL QUICK EXIT</button>
          <span className="muted">{auth.user.username} ({auth.user.role})</span>
          <button onClick={async () => { await api.post("/api/auth/logout"); setCsrf(""); loadAuth(); }}>Sign out</button>
        </div>
      </div>
      {alerts.filter((a) => a.severity === "CRITICAL").slice(0, 3).map((a) => (
        <div key={a.alert_id} className="alert-banner">
          <strong>{a.title}</strong> <span>{a.body}</span>
          <button onClick={() => { api.post(`/api/alerts/${a.alert_id}/ack`).catch(() => undefined); setAlerts((xs) => xs.filter((x) => x.alert_id !== a.alert_id)); }}>Acknowledge</button>
        </div>
      ))}
      {lastError && <div className="note err sticky">{lastError}</div>}
      <div className="layout">
        <nav>
          {NAV.map(([href, label]) => (
            <Link key={href} href={href} className={path === href || (href !== "/" && path?.startsWith(href)) ? "active" : ""}>{label}</Link>
          ))}
          <div className="nav-foot muted">Every order passes the deterministic Risk Kernel. AI output is advisory.</div>
        </nav>
        <main>{children}</main>
      </div>
      {stepUp && (
        <div className="modal">
          <div className="card">
            <h2>Confirm with your password</h2>
            <p className="muted">This action needs a fresh step-up (valid {auth.step_up_ttl_s}s).</p>
            <input type="password" autoFocus value={pw} onChange={(e) => setPw(e.target.value)} onKeyDown={(e) => e.key === "Enter" && doStepUp(true)} />
            <div className="row">
              <button className="primary" onClick={() => doStepUp(true)}>Confirm</button>
              <button onClick={() => doStepUp(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}
    </ShellCtx.Provider>
  );
}

export { DataLabelBadge };
