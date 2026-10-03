"use client";
import { useState } from "react";
import { useShell } from "@/components/Shell";
import { Badge, Card, Json, KV } from "@/components/ui";
import { api, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Risk = { safety: { kill_switch: Record<string, unknown>; freeze: { active: boolean; reasons: { code: string; by: string; at: number; detail: string }[] }; emergency: { level: string; reason?: string };
  recovery_lock: { active: boolean; incident_id?: string; reason?: string }; ai_suspended: { active: boolean; reason?: string } }; blockers: string[];
  path_a: Record<string, Record<string, unknown>>; path_b: { ts: number; code: string; detail: string }[]; watchdog: { at: number; reason: string }[] };
type Policies = { policies: { policy_id: string; kind: string; account_id: string; version: number; status: string; created_by: string; body: unknown }[]; hard_limits: Record<string, unknown> };
type Verify = { passed: boolean; failed: string[]; checks: { check: string; passed: boolean; detail: string }[] };

export default function RiskPage() {
  const { act, status } = useShell();
  const r = usePoll<Risk>("/api/risk", 2000);
  const pol = usePoll<Policies>("/api/policies", 10000);
  const [ver, setVer] = useState<Verify | null>(null);
  const [draft, setDraft] = useState('{\n  "account_id": "PAPER-1",\n  "loss_level1_inr": 2000,\n  "loss_level2_inr": 4000,\n  "margin_risk_level1_pct": 1,\n  "margin_risk_level2_pct": 2\n}');
  const [kind, setKind] = useState("risk");
  const s = r.data?.safety;
  return (
    <>
      <div className="grid">
        <Card title="Safety state (latched — only the owner releases, after verification)">
          <KV rows={[
            ["Kill switch", s?.kill_switch.engaged || s?.kill_switch.file_engaged ? <span key="k"><Badge v="BLOCKED" /> {String(s?.kill_switch.reason ?? "")}</span> : <Badge v="OK" />],
            ["Freeze new risk", s?.freeze.active ? s.freeze.reasons.map((x) => `${x.code} (${x.by})`).join(", ") : "no"],
            ["Emergency level", s?.emergency.level], ["Recovery lock", s?.recovery_lock.active ? `${s.recovery_lock.incident_id}: ${s.recovery_lock.reason}` : "no"],
            ["AI actions", s?.ai_suspended.active ? `SUSPENDED: ${s.ai_suspended.reason}` : "allowed"],
          ]} />
          <div className="row" style={{ marginTop: 10 }}>
            <button onClick={async () => setVer(await api.get<Verify>("/api/safety/verify"))}>Run recovery verification</button>
            <button onClick={() => act("/api/safety/kill/release", {}, { confirm: "Release the kill switch? Requires a passing verification." })}>Release kill switch</button>
            <button onClick={() => act("/api/safety/freeze/release", { codes: null }, { confirm: "Release all freeze reasons (not the kill switch)?" })}>Release freeze</button>
            <button onClick={() => act("/api/safety/recovery-lock/release", {}, { confirm: "Release the recovery lock?" })}>Release recovery lock</button>
            <button onClick={() => act("/api/safety/ai/resume", {}, { confirm: "Resume AI-driven actions?" })}>Resume AI actions</button>
          </div>
          {ver && <div style={{ marginTop: 10 }}><Badge v={ver.passed ? "OK" : "BLOCKED"} /> {ver.checks.map((c) => <div key={c.check}><Badge v={c.passed ? "OK" : "FAILED"} /> {c.check} <span className="muted">{c.detail}</span></div>)}</div>}
        </Card>
        <Card title="Mode">
          <KV rows={[["Current", <Badge key="m" v={status?.mode.mode} />], ["Deployment", status?.environment]]} />
          <p className="muted">Startup is always PAPER. MANUAL/AUTOMATIC need a LIVE_CAPABLE deployment and a passing readiness check. AUTOMATIC is entered only from MANUAL and needs the confirmation phrase.</p>
          <div className="row">
            {["PAPER", "MANUAL", "AUTOMATIC"].map((m) => (
              <button key={m} onClick={() => {
                const confirmation = m === "AUTOMATIC" ? window.prompt("Type: ENABLE AUTOMATIC MODE") ?? "" : "";
                act("/api/mode", { target: m, confirmation, reason: "owner request" }, { confirm: `Switch to ${m}?` });
              }}>{m}</button>))}
            <button onClick={() => act("/api/mode/paper-auto-approve", { on: !status?.mode.paper_auto_approve })}>Paper auto-approve: {status?.mode.paper_auto_approve ? "ON" : "off"}</button>
          </div>
        </Card>
      </div>
      <div className="grid">
        <Card title="Path A — portfolio risk monitor"><Json v={r.data?.path_a ?? {}} /></Card>
        <Card title="Path B — independent safety monitor findings">
          {(r.data?.path_b ?? []).slice(-20).reverse().map((f, i) => <div key={i}><span className="muted">{ts(f.ts)}</span> <Badge v="WARNING" /> {f.code} — {f.detail}</div>)}
          <h3>Watchdog trips</h3>{(r.data?.watchdog ?? []).map((w, i) => <div key={i}>{ts(w.at)} {w.reason}</div>)}
        </Card>
      </div>
      <Card title="Risk & automation policies (versioned; owner approval + step-up)">
        <table><thead><tr><th className="l">Policy</th><th className="l">Kind</th><th className="l">Account</th><th>Version</th><th className="l">Status</th><th className="l">By</th><th></th></tr></thead>
          <tbody>{(pol.data?.policies ?? []).map((p) => <tr key={p.policy_id}><td className="l">{p.policy_id}</td><td className="l">{p.kind}</td><td className="l">{p.account_id}</td><td>{p.version}</td>
            <td className="l"><Badge v={p.status === "ACTIVE" ? "OK" : p.status} /> {p.status}</td><td className="l">{p.created_by}</td>
            <td>{p.status === "DRAFT" && <button onClick={() => act(`/api/policies/${p.policy_id}/activate`, {}, { confirm: "Activate this policy version?" })}>Activate</button>}</td></tr>)}</tbody></table>
        <h3>Propose a new version</h3>
        <div className="row"><select value={kind} onChange={(e) => setKind(e.target.value)}><option value="risk">risk</option><option value="automation">automation</option></select>
          <span className="muted">Validated against deployment hard limits; it starts as DRAFT.</span></div>
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button className="primary" onClick={() => { try { act("/api/policies", { kind, body: JSON.parse(draft), note: "dashboard" }); } catch (e) { alert(String(e)); } }}>Propose</button>
        <h3>Deployment hard limits (change only by redeploying)</h3><Json v={pol.data?.hard_limits ?? {}} />
      </Card>
    </>
  );
}
