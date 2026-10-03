"use client";
import { useState } from "react";
import { useShell } from "@/components/Shell";
import { Badge, Card, Empty } from "@/components/ui";
import { fmt, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Order = { intent_id: string; account_id: string; instrument_key: string; side: string; quantity: number; filled_qty: number; avg_price: number; state: string;
  broker_order_id: string | null; client_tag: string; created_at: number; updated_at: number; last_error: string | null; simulated: boolean;
  intent: { order_type: string; limit_price: number | null; purpose: string; origin: string; reduce_only: boolean; decision_id: string | null } };

export default function Orders() {
  const { act, status } = useShell();
  const q = usePoll<Order[]>("/api/orders?limit=300", 3000);
  const [t, setT] = useState({ account_id: "", instrument_key: "", side: "BUY", lots: 1, order_type: "LIMIT", limit_price: "", reduce_only: false, note: "" });
  const submit = async () => {
    await act("/api/orders/manual", { ...t, account_id: t.account_id || status?.accounts[0]?.account_id, limit_price: t.limit_price ? Number(t.limit_price) : null },
      { confirm: `Send ${t.side} ${t.lots} lot(s) ${t.instrument_key}? The Risk Kernel evaluates it first.` });
    q.reload();
  };
  const working = (q.data ?? []).filter((o) => ["SUBMITTING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "ORDER STATE UNKNOWN"].includes(o.state));
  return (
    <>
      {working.some((o) => o.state === "ORDER STATE UNKNOWN") && <div className="note err">One or more orders are in ORDER STATE UNKNOWN. The system never assumes they failed; reconciliation resolves them and new risk on those instruments is blocked meanwhile.</div>}
      <Card title="Manual order ticket (owner)">
        <div className="row">
          <select value={t.account_id} onChange={(e) => setT({ ...t, account_id: e.target.value })}>
            {(status?.accounts ?? []).map((a) => <option key={a.account_id} value={a.account_id}>{a.account_id} ({a.kind})</option>)}
          </select>
          <input style={{ width: 300 }} placeholder="instrument key e.g. NSE:NIFTY:2026-10-06:25000:CE" value={t.instrument_key} onChange={(e) => setT({ ...t, instrument_key: e.target.value })} />
          <select value={t.side} onChange={(e) => setT({ ...t, side: e.target.value })}><option>BUY</option><option>SELL</option></select>
          <input type="number" min={1} style={{ width: 70 }} value={t.lots} onChange={(e) => setT({ ...t, lots: Number(e.target.value) })} />
          <select value={t.order_type} onChange={(e) => setT({ ...t, order_type: e.target.value })}><option>LIMIT</option><option>MARKET</option></select>
          {t.order_type === "LIMIT" && <input style={{ width: 90 }} placeholder="limit" value={t.limit_price} onChange={(e) => setT({ ...t, limit_price: e.target.value })} />}
          <label><input type="checkbox" checked={t.reduce_only} onChange={(e) => setT({ ...t, reduce_only: e.target.checked })} /> reduce-only (exit)</label>
          <button className="primary" onClick={submit}>Send for risk check</button>
        </div>
      </Card>
      <Card title={`Orders (${q.data?.length ?? 0})`}>
        {!q.data?.length ? <Empty>No orders.</Empty> : (
          <div className="scroll"><table><thead><tr><th className="l">Created</th><th className="l">Account</th><th className="l">Instrument</th><th className="l">Side</th><th>Qty</th><th>Filled</th><th>Avg</th><th>Limit</th><th className="l">Purpose / origin</th><th className="l">State</th><th className="l">Broker id</th><th className="l">Note</th><th></th></tr></thead>
            <tbody>{q.data.map((o) => (
              <tr key={o.intent_id}><td className="l">{ts(o.created_at)}</td><td className="l">{o.account_id}{o.simulated && <> <span className="badge b-purple">SIMULATED</span></>}</td><td className="l">{o.instrument_key}</td>
                <td className="l">{o.side}</td><td>{o.quantity}</td><td>{o.filled_qty}</td><td>{fmt(o.avg_price)}</td><td>{fmt(o.intent.limit_price)}</td>
                <td className="l">{o.intent.purpose} / {o.intent.origin}{o.intent.reduce_only ? " · reduce-only" : ""}</td><td className="l"><Badge v={o.state} /></td>
                <td className="l">{o.broker_order_id ?? "—"}</td><td className="l" style={{ whiteSpace: "normal", maxWidth: 260 }}>{o.last_error ?? ""}</td>
                <td>{["ACKNOWLEDGED", "PARTIALLY_FILLED"].includes(o.state) && <button onClick={() => act(`/api/orders/${o.intent_id}/cancel`, { reason: "owner cancel" }, { confirm: "Cancel this order?" })}>Cancel</button>}</td></tr>))}
            </tbody></table></div>)}
      </Card>
    </>
  );
}
