"use client";
import { useState } from "react";
import { Badge, Card, DataLabelBadge, Empty, Json, KV } from "@/components/ui";
import { fmt, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Spec = { agent: string; status: string; stance: string; confidence: number; confidence_method: string; warnings: string[]; inferences: string[]; errors: string[]; latency_ms: number };
type Pkg = {
  decision_id: string; status: string; created_at: number; expires_at: number; mode: string; account_id: string; underlying: string; market_regime: string;
  data: { label: string; source: string; age_ms: number | null; chain_status: string }; specialists: Spec[];
  agreement: { supportive: string[]; neutral: string[]; caution: string[]; blocking: string[]; disagreements: unknown[] };
  verification: { passed: boolean; checks: { check: string; passed: boolean; detail: string }[] };
  proposed_action: null | { kind: string; strategy_id: string; strategy_version: string; expiry: string; metrics: Record<string, unknown>; est_charges_inr: number;
    legs: { instrument_key: string; side: string; lots: number; order_type: string; limit_price: number | null; purpose: string; bid: number | null; ask: number | null }[] };
  risk_precheck: null | { approved: boolean | null; failed?: string[]; note?: string };
  rationale: string[]; risks: string[]; assumptions: string[]; invalidation: string[]; confidence: number; confidence_method: string; required_approvals: string[];
  reasons: string[]; routing: unknown; narrative?: { label: string; text: string | null; status: string } | null;
};

export default function Decisions() {
  const latest = usePoll<Record<string, Pkg>>("/api/decisions/latest", 5000);
  const list = usePoll<{ decision_id: string; ts: number; underlying: string; status: string }[]>("/api/decisions?limit=40", 10000);
  const [open, setOpen] = useState<string | null>(null);
  const detail = usePoll<Pkg & { timeline: unknown[] }>(open ? `/api/decisions/${open}` : null, 0);
  const pkgs = Object.values(latest.data ?? {});
  return (
    <>
      <div className="note">The Master AI Agent is advisory. It can recommend; only the owner (or a pre-approved automation policy) authorizes, and the Risk Kernel evaluates every order.</div>
      {!pkgs.length && <Empty>No decision packages yet.</Empty>}
      {pkgs.map((p) => (
        <Card key={p.decision_id} title={<>{p.underlying} · <Badge v={p.status} /> <DataLabelBadge label={p.data.label} /></>} right={<span className="muted">{p.decision_id} · {ts(p.created_at)} · expires {ts(p.expires_at)}</span>}>
          <div className="grid">
            <div>
              <KV rows={[["Mode / account", `${p.mode} / ${p.account_id}`], ["Market regime (inference)", p.market_regime], ["Chain status", p.data.chain_status],
                ["Confidence", <span key="c" title={p.confidence_method}>{p.confidence.toFixed(3)} <span className="muted">(heuristic)</span></span>],
                ["Required approvals", p.required_approvals.join(" + ") || "none"], ["Verification", <Badge key="v" v={p.verification.passed ? "OK" : "BLOCKED"} />],
                ["Risk pre-check", p.risk_precheck ? <Badge key="r" v={p.risk_precheck.approved === false ? "REJECTED" : p.risk_precheck.approved ? "OK" : "UNKNOWN"} /> : "not run"]]} />
              <h3>Reasons</h3><ul>{p.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              {p.narrative?.text && (<><h3>{p.narrative.label}</h3><p>{p.narrative.text}</p></>)}
            </div>
            <div>
              <h3>Agreement</h3>
              <KV rows={[["Supportive", p.agreement.supportive.join(", ") || "—"], ["Caution", p.agreement.caution.join(", ") || "—"], ["Blocking", p.agreement.blocking.join(", ") || "—"]]} />
              {p.proposed_action ? (
                <>
                  <h3>Proposed action: {p.proposed_action.kind} {p.proposed_action.strategy_id} v{p.proposed_action.strategy_version} (exp {p.proposed_action.expiry})</h3>
                  <table><thead><tr><th className="l">Leg</th><th className="l">Side</th><th>Lots</th><th>Limit</th><th>Bid</th><th>Ask</th><th className="l">Purpose</th></tr></thead>
                    <tbody>{p.proposed_action.legs.map((l) => <tr key={l.instrument_key + l.side}><td className="l">{l.instrument_key}</td><td className="l">{l.side}</td><td>{l.lots}</td><td>{fmt(l.limit_price)}</td><td>{fmt(l.bid)}</td><td>{fmt(l.ask)}</td><td className="l">{l.purpose}</td></tr>)}</tbody></table>
                  <KV rows={Object.entries(p.proposed_action.metrics).map(([k, v]) => [k, typeof v === "object" ? JSON.stringify(v) : String(v ?? "—")] as [string, string]).concat([["est. charges", `₹${fmt(p.proposed_action.est_charges_inr)}`]])} />
                  {p.risk_precheck?.failed?.length ? <div className="note err">{p.risk_precheck.failed.join("; ")}</div> : null}
                </>) : <p className="muted">No action proposed.</p>}
            </div>
          </div>
          <h3>Specialist agents</h3>
          <table><thead><tr><th className="l">Agent</th><th className="l">Status</th><th className="l">Stance</th><th>Confidence</th><th>ms</th><th className="l">Warnings / errors</th></tr></thead>
            <tbody>{p.specialists.map((s) => (
              <tr key={s.agent}><td className="l">{s.agent}</td><td className="l"><Badge v={s.status} /></td><td className="l"><Badge v={s.stance} /></td>
                <td title={s.confidence_method}>{s.confidence.toFixed(2)}</td><td>{fmt(s.latency_ms, 1)}</td><td className="l" style={{ whiteSpace: "normal" }}>{[...s.errors, ...s.warnings].join(" · ")}</td></tr>))}
            </tbody></table>
          <h3>Risks</h3><ul>{p.risks.slice(0, 12).map((r) => <li key={r}>{r}</li>)}</ul>
          <h3>Invalidation conditions</h3><ul>{p.invalidation.map((r) => <li key={r}>{r}</li>)}</ul>
          <button onClick={() => setOpen(p.decision_id)}>Full package & audit timeline</button>
        </Card>
      ))}
      <Card title="History">
        <table><thead><tr><th className="l">Decision</th><th className="l">Underlying</th><th className="l">Status</th><th>At</th></tr></thead>
          <tbody>{(list.data ?? []).map((d) => <tr key={d.decision_id} onClick={() => setOpen(d.decision_id)} style={{ cursor: "pointer" }}><td className="l">{d.decision_id}</td><td className="l">{d.underlying}</td><td className="l"><Badge v={d.status} /></td><td>{ts(d.ts)}</td></tr>)}</tbody></table>
      </Card>
      {open && detail.data && <Card title={`Package ${open}`} right={<button onClick={() => setOpen(null)}>close</button>}><Json v={detail.data} /></Card>}
    </>
  );
}
