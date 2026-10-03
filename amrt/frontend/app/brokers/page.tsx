"use client";
import { Badge, Card } from "@/components/ui";
import { usePoll } from "@/lib/hooks";

type Cap = { feature: string; status: string; notes: string; official_reference: string };
type Prof = { name: string; display: string; sdk_package: string; sdk_version_tested: string; official_docs: string; api_version: string; auth: string; overall_status: string; capabilities: Cap[] };
type B = { capability_matrix: Prof[]; status: Record<string, unknown>[]; data_broker: string | null };

export default function Brokers() {
  const q = usePoll<B>("/api/brokers", 15000);
  return (
    <>
      <div className="note">Only officially documented broker features are used. VERIFIED means exercised against the live API with evidence; PARTIAL means implemented against the official SDK but not verified live from this environment; BLOCKED / UNAVAILABLE are not used.</div>
      {(q.data?.capability_matrix ?? []).map((p) => {
        const st = (q.data?.status ?? []).find((s) => s.broker === p.name) ?? {};
        return (
          <Card key={p.name} title={<>{p.display} <Badge v={p.overall_status} /></>} right={<span className="muted">{p.sdk_package} {p.sdk_version_tested} · API {p.api_version} · configured: {String(st.configured)} · authenticated: {String(st.authenticated)}</span>}>
            <div className="muted">Auth: {p.auth} · Docs: {p.official_docs}</div>
            <table><thead><tr><th className="l">Feature</th><th className="l">Status</th><th className="l">Notes</th></tr></thead>
              <tbody>{(p.capabilities ?? []).map((c) => <tr key={c.feature}><td className="l">{c.feature}</td><td className="l"><Badge v={c.status} /></td><td className="l" style={{ whiteSpace: "normal" }}>{c.notes}</td></tr>)}</tbody></table>
          </Card>);
      })}
    </>
  );
}
