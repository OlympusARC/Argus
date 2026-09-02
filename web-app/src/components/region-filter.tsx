"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { REGIONS } from "@/lib/taxonomy";

/**
 * Regions are multi-select, unlike the other filters, because they are not
 * mutually exclusive in the way a family or a level is: "US or Europe" is
 * the ordinary question, and "US" alone is the exception.
 *
 * Repeated params rather than a comma-joined string -- ?region=us&region=europe
 * -- so the URL says what it means and getAll does the parsing.
 */
export function RegionFilter() {
  const router = useRouter();
  const params = useSearchParams();
  const selected = params.getAll("region");

  const toggle = (value: string) => {
    const next = new URLSearchParams(params.toString());
    const now = selected.includes(value)
      ? selected.filter((v) => v !== value)
      : [...selected, value];
    next.delete("region");
    now.forEach((v) => next.append("region", v));
    next.delete("page");
    router.push(`/?${next.toString()}`, { scroll: false });
  };

  const label =
    selected.length === 0
      ? "Region: any"
      : selected.length === 1
        ? (REGIONS.find((r) => r.value === selected[0])?.label ?? "Region")
        : `${selected.length} regions`;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          className="h-9 w-[9.5rem] justify-between font-normal"
        >
          {label}
          <ChevronDown className="size-4 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-56 p-1.5">
        {REGIONS.map((r) => (
          <label
            key={r.value}
            className="flex cursor-pointer items-center gap-2.5 rounded-sm px-2 py-2 text-sm hover:bg-accent"
          >
            <Checkbox
              checked={selected.includes(r.value)}
              onCheckedChange={() => toggle(r.value)}
            />
            {r.label}
          </label>
        ))}
      </PopoverContent>
    </Popover>
  );
}
