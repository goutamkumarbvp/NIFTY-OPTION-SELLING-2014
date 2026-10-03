"use client";
import { useShell } from "@/components/Shell";
import { Badge, Card, Empty } from "@/components/ui";
import { fmt, ts } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

type Approval = { approval_id: string; decision_id: string; status: string; created_at: number; expires_at: number; decided_by: string | null;
  body: { account_id: string; mode: string; proposed_action: { kind: string; strategy_id: string; legs: { instrument_key: string; side: string; lots: number; limit_price: number | null; purpose: string }[] } | null } };

export default function Approvals() {
  const { act } = useShell();
  const q = usePoll<{ pending: Approval[]; history: Approval[] }>("/api/approvals", 3000);
  return (
    <>
      <div className="note">Accepting sends each leg through the Risk Kernel and the Execution Gateway — hedges first, short legs last. If a leg fails, the remaining legs are not sent and you are alerted. Accepting needs a fresh password step-up.</div>
      <Card title="Pending approvals">
        {!q.data?.pending.length ? <Empty>Nothing waiting for approval.</Empty> : q.data.pending.map((a) => (
          <div key={a.approval_id} className="card" style={{ marginBottom: 10 }}>
            <div className="row"><strong>{a.body.proposed_action?.kind} {a.body.proposed_action?.strategy_id}</strong><Badge v={a.status} />
              <span className="muted">account {a.body.account_id} · mode {a.body.mode} · expires {ts(a.expires_at)} · decision {a.decision_id}</span></div>
            <table><thead><tr><th className="l">Instrument</th><th className="l">Side</th><th>Lots</th><th>Limit</th><th className="l">Purpose</th></tr></thead>
              <tbody>{(a.body.proposed_action?.legs ?? []).map((l) => <tr key={l.instrument_key + l.side}><td className="l">{l.instrument_key}</td><td className="l">{l.side}</td><td>{l.lots}</td><td>{fmt(l.limit_price)}</td><td className="l">{l.purpose}</td></tr>)}</tbody></table>
            <div className="row" style={{ marginTop: 8 }}>
              <button className="primary" onClick={async () => { await act(`/api/approvals/${a.approval_id}/accept`, {}, { confirm: "Send these orders? Each one is evaluated by the Risk Kernel." }); q.reload(); }}>Accept</button>
              <button onClick={async () => { const r = window.prompt("Reason for rejecting"); if (r !== null) { await act(`/api/approvals/${a.approval_id}/reject`, { reason: r }); q.reload(); } }}>Reject</button>
            </div>
          </div>))}
      </Card>
      <Card title="History">
        <table><thead><tr><th className="l">Approval</th><th className="l">Status</th><th className="l">By</th><th className="l">Account</th><th>Created</th></tr></thead>
          <tbody>{(q.data?.history ?? []).map((a) => <tr key={a.approval_id}><td className="l">{a.approval_id}</td><td className="l"><Badge v={a.status} /></td><td className="l">{a.decided_by ?? "—"}</td><td className="l">{a.body.account_id}</td><td>{ts(a.created_at)}</td></tr>)}</tbody></table>
      </Card>
    </>
  );
}
