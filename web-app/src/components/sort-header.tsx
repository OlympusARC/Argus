"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import { DEFAULT_SORT, type SortKey } from "@/lib/taxonomy";
import { cn } from "@/lib/utils";

/**
 * A sortable column header. Like the filters, the state is a URL parameter
 * rather than React state, so a sorted view can be linked and the table
 * stays a server component.
 *
 * The header row is uniformly white, so which column is sorted is carried by
 * the arrow alone -- inactive columns show theirs on hover only, which is
 * enough to say a column is clickable without dimming its label.
 */
export function SortHeader({
  column,
  children,
  align = "left",
}: {
  column: SortKey;
  children: React.ReactNode;
  align?: "left" | "right";
}) {
  const router = useRouter();
  const params = useSearchParams();

  const current = (params.get("sort") as SortKey | null) ?? DEFAULT_SORT;
  const desc = params.get("dir") !== "asc";
  const active = current === column;

  const toggle = () => {
    const next = new URLSearchParams(params.toString());
    /**
     * Clicking the active column flips direction; clicking a new one starts
     * descending, which for a date means newest first -- the answer people
     * want without a second click.
     */
    if (active) {
      next.set("dir", desc ? "asc" : "desc");
    } else {
      next.set("sort", column);
      next.set("dir", "desc");
    }
    next.delete("page");
    router.push(`/?${next.toString()}`, { scroll: false });
  };

  const Icon = !active ? ChevronsUpDown : desc ? ArrowDown : ArrowUp;

  return (
    <button
      type="button"
      onClick={toggle}
      className={cn(
        /**
         * `uppercase` is repeated here rather than left to the header row.
         * text-transform inherits everywhere except into form controls,
         * which the UA stylesheet resets -- so the th went to caps and the
         * button inside it did not.
         */
        "group inline-flex items-center gap-1 uppercase",
        align === "right" && "flex-row-reverse",
      )}
    >
      {children}
      <Icon
        className={cn(
          "size-3 transition-opacity",
          active ? "opacity-80" : "opacity-0 group-hover:opacity-40",
        )}
      />
    </button>
  );
}
