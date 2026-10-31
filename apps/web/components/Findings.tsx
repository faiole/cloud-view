import type { Finding } from "@/lib/api";

export default function Findings({ items }: { items: Finding[] }) {
  if (!items?.length) return null;
  return (
    <ul className="findings">
      {items.map((f, i) => (
        <li key={f.code + i} className={`sev-${f.severity}`}>{f.message}</li>
      ))}
    </ul>
  );
}
