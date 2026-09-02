import Link from "next/link";
import { ArrowUpRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { Job } from "@/lib/jobs";

/**
 * One hue per family, at low saturation. The families are categories rather
 * than severities, so nothing here should read as good or bad -- the colour
 * exists to let the eye group rows, not to rank them.
 */
const FAMILY_TONE: Record<string, string> = {
  engineering: "text-sky-700 dark:text-sky-300 bg-sky-500/8 border-sky-500/20",
  fde: "text-violet-700 dark:text-violet-300 bg-violet-500/8 border-violet-500/20",
  ai: "text-teal-700 dark:text-teal-300 bg-teal-500/8 border-teal-500/20",
  data: "text-amber-700 dark:text-amber-300 bg-amber-500/8 border-amber-500/20",
  security: "text-rose-700 dark:text-rose-300 bg-rose-500/8 border-rose-500/20",
  product: "text-emerald-700 dark:text-emerald-300 bg-emerald-500/8 border-emerald-500/20",
};

function ago(ts: number | null) {
  if (!ts) return "—";
  const mins = Math.max(0, Math.floor(Date.now() / 1000 - ts) / 60);
  if (mins < 60) return `${Math.floor(mins)}m`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h`;
  const d = Math.floor(mins / 1440);
  return d < 30 ? `${d}d` : `${Math.floor(d / 30)}mo`;
}

export function JobTable({ jobs }: { jobs: Job[] }) {
  if (jobs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed py-16 text-center">
        <p className="text-sm font-medium">No roles match these filters</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Try clearing one — the corpus only stores engineering, product and adjacent families.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border">
      <Table className="table-fixed">
        <TableHeader>
          <TableRow className="hover:bg-transparent">
            <TableHead className="w-[42%]">Role</TableHead>
            <TableHead className="w-[18%]">Company</TableHead>
            <TableHead className="w-[20%]">Location</TableHead>
            <TableHead className="w-[12%]">Family</TableHead>
            <TableHead className="w-[8%] text-right">Seen</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {jobs.map((j) => (
            <TableRow key={`${j.ats}:${j.slug}:${j.external_id}`} className="group">
              <TableCell className="py-2.5">
                {j.url ? (
                  <Link
                    href={j.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-start gap-1 font-medium underline-offset-4 hover:underline"
                  >
                    <span className="line-clamp-2">{j.title}</span>
                    <ArrowUpRight className="mt-0.5 size-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-60" />
                  </Link>
                ) : (
                  <span className="font-medium">{j.title}</span>
                )}
                {j.seniority && (
                  <span className="ml-2 text-xs text-muted-foreground capitalize">
                    {j.seniority.replace("_", " ")}
                  </span>
                )}
              </TableCell>
              <TableCell className="text-muted-foreground">
                <div className="truncate">{j.company ?? j.slug}</div>
              </TableCell>
              <TableCell className="text-muted-foreground">
                <div className="truncate">{j.location ?? "—"}</div>
              </TableCell>
              <TableCell>
                {j.role_family && (
                  <Badge
                    variant="outline"
                    className={`font-normal capitalize ${FAMILY_TONE[j.role_family] ?? ""}`}
                  >
                    {j.role_family}
                  </Badge>
                )}
              </TableCell>
              <TableCell className="text-right text-xs text-muted-foreground tabular-nums">
                {ago(j.first_seen_at)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
