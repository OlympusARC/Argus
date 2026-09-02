"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, useTransition } from "react";
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ATSES, FAMILIES, SENIORITIES } from "@/lib/taxonomy";

const ANY = "__any";

/**
 * Filter state lives in the URL, not in React.
 *
 * That keeps the table a server component -- it reads searchParams and
 * queries Postgres directly, so no job data is ever shipped to the client to
 * be filtered there. It also makes every view linkable, which is the point of
 * a dashboard someone wants to share.
 */
export function Filters({ total }: { total: number }) {
  const router = useRouter();
  const params = useSearchParams();
  const [pending, startTransition] = useTransition();
  const [q, setQ] = useState(params.get("q") ?? "");
  const [co, setCo] = useState(params.get("company") ?? "");

  const push = useCallback(
    (next: URLSearchParams) => {
      next.delete("page");
      startTransition(() => router.push(`/?${next.toString()}`, { scroll: false }));
    },
    [router],
  );

  const set = (key: string, value: string) => {
    const next = new URLSearchParams(params.toString());
    if (!value || value === ANY) next.delete(key);
    else next.set(key, value);
    push(next);
  };

  /**
   * Debounced, because a query per keystroke is an ILIKE over 127k rows per
   * keystroke.
   */
  useEffect(() => {
    const id = setTimeout(() => {
      if ((params.get("q") ?? "") === q && (params.get("company") ?? "") === co) return;
      const next = new URLSearchParams(params.toString());
      if (q) next.set("q", q);
      else next.delete("q");
      if (co) next.set("company", co);
      else next.delete("company");
      push(next);
    }, 350);
    return () => clearTimeout(id);
  }, [q, co, params, push]);

  const active = ["family", "seniority", "ats", "q", "company"].filter((k) => params.get(k));

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <Input
          placeholder="Search roles…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="h-9 w-full sm:w-64"
        />
        <Input
          placeholder="Search companies…"
          value={co}
          onChange={(e) => setCo(e.target.value)}
          className="h-9 w-full sm:w-56"
        />
        <Picker
          label="Type"
          value={params.get("family")}
          options={FAMILIES}
          onChange={(v) => set("family", v)}
        />
        <Picker
          label="Level"
          value={params.get("seniority")}
          options={SENIORITIES}
          onChange={(v) => set("seniority", v)}
        />
        <Picker
          label="Source"
          value={params.get("ats")}
          options={ATSES}
          onChange={(v) => set("ats", v)}
        />

        {active.length > 0 && (
          <Button
            variant="ghost"
            size="sm"
            className="h-9 text-muted-foreground"
            onClick={() => {
              setQ("");
              setCo("");
              push(new URLSearchParams());
            }}
          >
            <X className="size-3.5" />
            Clear
          </Button>
        )}
      </div>

      <p
        className="text-xs text-muted-foreground tabular-nums transition-opacity"
        style={{ opacity: pending ? 0.4 : 1 }}
      >
        {total.toLocaleString()} {active.length > 0 ? "matching" : "open"}{" "}
        {total === 1 ? "role" : "roles"}
      </p>
    </div>
  );
}

function Picker({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string | null;
  options: readonly string[];
  onChange: (v: string) => void;
}) {
  return (
    <Select value={value ?? ANY} onValueChange={onChange}>
      <SelectTrigger className="h-9 w-[9.5rem] capitalize" size="sm">
        <SelectValue placeholder={label} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ANY}>{label}: any</SelectItem>
        {options.map((o) => (
          <SelectItem key={o} value={o} className="capitalize">
            {o.replace("_", " ")}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
