"use client";

import Link from "next/link";
import { ArrowUpRight, EyeOff, MoreHorizontal, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { jobKey, useJobState } from "@/lib/use-job-state";

import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortHeader } from "@/components/sort-header";
import type { Job } from "@/lib/jobs";

/**
 * One hue per family, at low saturation. The families are categories rather
 * than severities, so nothing here should read as good or bad -- the colour
 * exists to let the eye group rows, not to rank them.
 */
const TYPE_TONE: Record<string, string> = {
  engineering: "text-sky-300 bg-sky-400/10 border-sky-400/20",
  fde: "text-violet-300 bg-violet-400/10 border-violet-400/20",
  ai: "text-teal-300 bg-teal-400/10 border-teal-400/20",
  data: "text-amber-300 bg-amber-400/10 border-amber-400/20",
};

function ago(ts: number | null) {
  if (!ts) return "—";
  const mins = Math.max(0, Math.floor(Date.now() / 1000 - ts) / 60);
  if (mins < 60) return `${Math.floor(mins)}m`;
  if (mins < 1440) return `${Math.floor(mins / 60)}h`;
  const d = Math.floor(mins / 1440);
  return d < 30 ? `${d}d` : `${Math.floor(d / 30)}mo`;
}

/**
 * Formatted in UTC, not the viewer's zone.
 *
 * posted_at is a UTC epoch and the ingest cutoff is a UTC midnight, so a
 * posting four minutes past the cutoff rendered as the previous evening in
 * EDT: the oldest row read "9 Aug" under a 10 Aug rule, and the boundary
 * looked broken. The dates carry no time of day worth localising -- the
 * fallback is a whole day, and a board's own date is a day too -- so the
 * only thing a local zone can do here is shift them.
 */
const DATE = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

/**
 * Posted is the date the board itself claims, so it is shown as a date. Seen
 * is when this pipeline first observed the posting, so it is shown as an age.
 * Two different facts, deliberately not rendered the same way.
 *
 * Workday and BambooHR expose no posted date at all -- 55,172 of 127,315 open
 * roles -- and those get an em dash rather than a fabricated one.
 */
function posted(ts: number | null) {
  if (!ts) return null;
  return DATE.format(new Date(ts * 1000));
}

export function JobTable({ jobs }: { jobs: Job[] }) {
  const { applied, dismissed, toggleApplied, dismiss, restoreAll } = useJobState();

  const visible = jobs.filter((j) => !dismissed.has(jobKey(j)));
  const hiddenHere = jobs.length - visible.length;

  if (jobs.length === 0) {
    return (
      <div className="rounded-lg border border-dashed py-16 text-center">
        <p className="text-sm font-medium">No roles match these filters</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Try clearing one — the corpus only stores engineering, AI, data and FDE roles.
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {hiddenHere > 0 && (
        <div className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
          {hiddenHere} hidden on this page
          <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={restoreAll}>
            <RotateCcw className="size-3" />
            Restore all
          </Button>
        </div>
      )}
      <div className="overflow-x-auto rounded-lg border">
      <Table className="table-fixed">
        <TableHeader>
          <TableRow className="hover:bg-transparent [&>th]:text-xs [&>th]:font-bold [&>th]:tracking-wider [&>th]:text-foreground [&>th]:uppercase">
            <TableHead className="w-[38%]">
              <SortHeader column="title">Role</SortHeader>
            </TableHead>
            <TableHead className="w-[12%]">
              <SortHeader column="company">Company</SortHeader>
            </TableHead>
            <TableHead className="w-[12%]">Location</TableHead>
            <TableHead className="w-[8%]">Type</TableHead>
            <TableHead className="w-[6%]">Applied</TableHead>
            <TableHead className="w-[6%]">
              <SortHeader column="source">Source</SortHeader>
            </TableHead>
            <TableHead className="w-[10%] text-right">
              <SortHeader column="posted" align="right">
                Posted
              </SortHeader>
            </TableHead>
            <TableHead className="w-[5%] text-right">
              <SortHeader column="seen" align="right">
                Seen
              </SortHeader>
            </TableHead>
            <TableHead className="w-[3%]" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {visible.map((j) => {
            const key = jobKey(j);
            const isApplied = applied.has(key);
            return (
            <TableRow
              key={key}
              className={`group ${isApplied ? "opacity-45" : ""}`}
            >
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
                  <span className="ml-2 text-xs whitespace-nowrap text-muted-foreground/70 capitalize">
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
                    className={`font-normal capitalize ${TYPE_TONE[j.role_family] ?? ""}`}
                  >
                    {j.role_family}
                  </Badge>
                )}
              </TableCell>
              <TableCell>
                <Checkbox
                  checked={isApplied}
                  onCheckedChange={() => toggleApplied(key)}
                  aria-label={`Mark ${j.title} as applied`}
                />
              </TableCell>
              <TableCell className="text-xs text-muted-foreground/80">
                {j.ats}
              </TableCell>
              <TableCell className="text-right text-xs tabular-nums">
                {posted(j.posted_at) ?? (
                  <span className="text-muted-foreground/40" title="this ATS exposes no posted date">
                    —
                  </span>
                )}
              </TableCell>
              <TableCell className="text-right text-xs text-muted-foreground tabular-nums">
                {ago(j.first_seen_at)}
              </TableCell>
              <TableCell className="text-right">
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="size-7 p-0 opacity-30 transition-opacity group-hover:opacity-80 data-[state=open]:opacity-100"
                      aria-label="Row actions"
                    >
                      <MoreHorizontal className="size-4" />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-40">
                    <DropdownMenuItem onClick={() => dismiss(key)}>
                      <EyeOff className="size-3.5" />
                      Hide this role
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </TableCell>
            </TableRow>
            );
          })}
        </TableBody>
      </Table>
      </div>
    </div>
  );
}
