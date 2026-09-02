import type { Stats } from "@/lib/jobs";

/**
 * Four numbers, no cards. A dashboard header that competes with the table
 * for attention is a dashboard where the table is harder to read, and the
 * table is the product.
 */
export function StatBar({ stats }: { stats: Stats }) {
  const items = [
    { label: "open roles", value: stats.total },
    { label: "active boards", value: stats.boards },
    { label: "companies", value: stats.companies },
    { label: "engineering", value: stats.families.find((f) => f.role_family === "engineering")?.n ?? 0 },
  ];

  return (
    <dl className="flex flex-wrap gap-x-10 gap-y-4">
      {items.map((i) => (
        <div key={i.label}>
          <dd className="text-2xl font-semibold tracking-tight tabular-nums">
            {i.value.toLocaleString()}
          </dd>
          <dt className="mt-0.5 text-xs tracking-wide text-muted-foreground uppercase">
            {i.label}
          </dt>
        </div>
      ))}
    </dl>
  );
}
