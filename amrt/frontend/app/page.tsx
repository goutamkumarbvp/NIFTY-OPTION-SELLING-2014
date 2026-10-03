"use client";
import Link from "next/link";
import { useShell } from "@/components/Shell";
import { Badge, Card, DataLabelBadge, Empty, KV } from "@/components/ui";
import { inr, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Portfolio = { account: { account_id: string; kind: string; display: string }; label: string; valuation: { net_pnl_today: number | null; open_lots: number; marks_missing: string[] }; risk: { level?: string; margin_risk_pct?: number } | null }[];
type Decisions = Record<string, { decision_id: string; status: string; created_at: number; data: { label: string }; confidence: number; reasons: string[]; market_regime: string }>;
type Health = { health: { name: string; state: string; critical: boolean; quarantined: boolean }[]; incidents: { incident_id: string; severity: string; title: string; status: string }[] };

export default function Overview() {
  const { status } = useShell();
  const pf = usePoll<Portfolio>("/api/portfolio", 3000);
  const dec = usePoll<Decisions>("/api/decisions/latest", 5000);
  const rel = usePoll<Health>("/api/reliability", 5000);
  const bad = (rel.data?.health ?? []).filter((h) => !["HEALTHY", "DEGRADED"].includes(h.state));
  return (
    <>
      <div className="grid">
        <Card title="System">
          <KV rows={[
            ["Mode", <Badge key="m" v={status?.mode.mode} />],
            ["Environment", status?.environment],
            ["Readiness", <span key="r"><Badge v={status?.readiness.status} /> <span className="muted">{status?.readiness.reason}</span></span>],
            ["Market data source", status?.data_source ?? "none (DATA UNAVAILABLE)"],
            ["Paper auto-approve", status?.mode.paper_auto_approve ? "ON (PAPER accounts only)" : "off"],
            ["New-risk blockers", (status?.blockers ?? []).join(", ") || "none"],
          ]} />
        </Card>
        <Card title="Accounts">
          {!pf.data ? <Empty>Loading…</Empty> : pf.data.map((p) => (
            <KV key={p.account.account_id} rows={[
              [p.account.account_id, <span key="k"><Badge v={p.label === "SIMULATED" ? "SIMULATED" : "LIVE"} /> {p.account.display}</span>],
              ["Net P&L today", <span key="p" className={(p.valuation.net_pnl_today ?? 0) < 0 ? "neg" : "pos"}>{inr(p.valuation.net_pnl_today)}</span>],
              ["Open lots", p.valuation.open_lots], ["Risk level", p.risk?.level ?? "—"],
            ]} />
          ))}
        </Card>
        <Card title="Health" right={<Link href="/reliability/">details</Link>}>
          {bad.length === 0 ? <div className="ok">All registered components healthy or degraded.</div> :
            bad.map((h) => <div key={h.name}><Badge v={h.state} /> {h.name} {h.critical && <span className="badge b-red">critical</span>}</div>)}
          <h3>Open incidents</h3>
          {(rel.data?.incidents ?? []).filter((i) => i.status !== "RESOLVED").slice(0, 6).map((i) => <div key={i.incident_id}><Badge v={i.severity} /> {i.title} <Badge v={i.status} /></div>)}
        </Card>
      </div>
      <Card title="Latest AI decision packages (advisory)" right={<Link href="/decisions/">open</Link>}>
        {!dec.data || !Object.keys(dec.data).length ? <Empty>No decision cycle has completed yet.</Empty> : (
          <table><thead><tr><th className="l">Underlying</th><th className="l">Status</th><th className="l">Data</th><th className="l">Regime</th><th>Confidence</th><th className="l">Main reason</th><th>At</th></tr></thead>
            <tbody>{Object.entries(dec.data).map(([u, d]) => (
              <tr key={u}><td className="l">{u}</td><td className="l"><Badge v={d.status} /></td><td className="l"><DataLabelBadge label={d.data.label} /></td>
                <td className="l">{d.market_regime}</td><td>{d.confidence.toFixed(2)}</td><td className="l">{d.reasons[d.reasons.length - 1]}</td><td>{ts(d.created_at)}</td></tr>))}
            </tbody></table>)}
      </Card>
    </>
  );
}
