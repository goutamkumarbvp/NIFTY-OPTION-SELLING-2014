"use client";
import { Badge, Card, Empty, KV } from "@/components/ui";
import { fmt, inr, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Pos = { instrument_key: string; net_qty: number; avg_price: number; mark: number | null; unrealized_pnl: number | null; lots: number; realized_pnl: number; simulated: boolean };
type P = { account: { account_id: string; kind: string; broker: string; display: string; funds: Record<string, number | string | null> }; label: string;
  valuation: { positions: Pos[]; realized_today: number; charges_today: number; unrealized: number | null; net_pnl_today: number | null; marks_missing: string[]; gross_notional: number | null; premium_exposure: number | null; open_lots: number };
  risk: Record<string, unknown> | null; reconciliation: { last_ok_ts: number | null; divergence: boolean; last_error: string | null }; policy: { label: string } };

export default function Portfolio() {
  const q = usePoll<P[]>("/api/portfolio", 2000);
  if (!q.data) return <Empty>Loading…</Empty>;
  return (
    <>
      {q.data.map((p) => (
        <Card key={p.account.account_id} title={<>{p.account.account_id} <Badge v={p.label === "SIMULATED" ? "SIMULATED" : "LIVE"} /> {p.account.display}</>} right={<span className="muted">policy {p.policy.label}</span>}>
          {p.valuation.marks_missing.length > 0 && <div className="note err">P&L UNAVAILABLE — no fresh mark for {p.valuation.marks_missing.join(", ")}. Missing data is never treated as zero; new risk is frozen.</div>}
          <div className="grid">
            <KV rows={[["Net P&L today", <span key="n" className={(p.valuation.net_pnl_today ?? 0) < 0 ? "neg" : "pos"}>{inr(p.valuation.net_pnl_today)}</span>], ["Realized", inr(p.valuation.realized_today)],
              ["Unrealized", inr(p.valuation.unrealized)], ["Charges", inr(p.valuation.charges_today)], ["Open lots", p.valuation.open_lots]]} />
            <KV rows={[["Premium exposure", inr(p.valuation.premium_exposure)], ["Gross notional", inr(p.valuation.gross_notional)], ["Funds (available)", inr(p.account.funds.available as number)],
              ["SOD funds (margin-risk denominator)", inr(p.account.funds.sod_funds as number)], ["Margin used", inr(p.account.funds.margin_used as number)]]} />
            <KV rows={[["Risk level (Path A)", String(p.risk?.level ?? "—")], ["Loss", inr(p.risk?.loss as number)], ["Margin-risk %", fmt(p.risk?.margin_risk_pct as number, 3)],
              ["Reconciled", ts(p.reconciliation.last_ok_ts)], ["Divergence", p.reconciliation.divergence ? <span key="d" className="badge b-red">YES</span> : "no"]]} />
          </div>
          {!p.valuation.positions.length ? <Empty>No open positions.</Empty> : (
            <table><thead><tr><th className="l">Instrument</th><th>Net qty</th><th>Lots</th><th>Avg</th><th>Mark</th><th>Unrealized</th></tr></thead>
              <tbody>{p.valuation.positions.map((x) => <tr key={x.instrument_key}><td className="l">{x.instrument_key}</td><td>{x.net_qty}</td><td>{x.lots}</td><td>{fmt(x.avg_price)}</td>
                <td>{x.mark === null ? <span className="badge b-red">DATA UNAVAILABLE</span> : fmt(x.mark)}</td><td className={(x.unrealized_pnl ?? 0) < 0 ? "neg" : "pos"}>{inr(x.unrealized_pnl)}</td></tr>)}</tbody></table>)}
        </Card>))}
      <div className="muted">Loss thresholds are triggers, not guaranteed maximum losses: gaps, slippage, halts and API failures can produce larger losses.</div>
    </>
  );
}
