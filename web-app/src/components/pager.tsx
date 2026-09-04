"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PAGE_SIZE } from "@/lib/taxonomy";
import { cn } from "@/lib/utils";

/**
 * The window of page numbers to show around the current one.
 *
 * 48,804 roles is 977 pages, so a flat list is out. First and last are always
 * reachable because "how far does this go" and "jump to the end" are the two
 * questions a number strip answers that Next alone does not.
 */
const AROUND = 2;

function pages(current: number, last: number): (number | "gap")[] {
  const keep = new Set<number>([1, last]);
  for (let i = current - AROUND; i <= current + AROUND; i++) {
    if (i >= 1 && i <= last) keep.add(i);
  }
  const sorted = [...keep].sort((a, b) => a - b);

  /**
   * A gap stands for at least two missing pages. Collapsing a single missing
   * page to an ellipsis would be wider than the number it replaces.
   */
  const out: (number | "gap")[] = [];
  sorted.forEach((n, i) => {
    if (i > 0 && n - sorted[i - 1] > 1) out.push("gap");
    out.push(n);
  });
  return out;
}

export function Pager({
  page,
  hasMore,
  total,
}: {
  page: number;
  hasMore: boolean;
  total: number;
}) {
  const router = useRouter();
  const params = useSearchParams();
  const last = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const go = (n: number) => {
    const next = new URLSearchParams(params.toString());
    if (n <= 1) next.delete("page");
    else next.set("page", String(n));
    router.push(`/?${next.toString()}`, { scroll: false });
  };

  if (page === 1 && !hasMore) return null;

  return (
    <nav aria-label="Pagination" className="flex items-center justify-center gap-1">
      <Button
        variant="ghost"
        size="sm"
        disabled={page <= 1}
        onClick={() => go(page - 1)}
        aria-label="Previous page"
      >
        <ChevronLeft className="size-4" />
      </Button>

      {pages(page, last).map((p, i) =>
        p === "gap" ? (
          <span key={`gap${i}`} className="px-1.5 text-muted-foreground select-none">
            …
          </span>
        ) : (
          <Button
            key={p}
            variant={p === page ? "outline" : "ghost"}
            size="sm"
            onClick={() => go(p)}
            aria-current={p === page ? "page" : undefined}
            className={cn(
              "min-w-9 tabular-nums",
              p === page ? "font-medium" : "text-muted-foreground",
            )}
          >
            {p.toLocaleString()}
          </Button>
        ),
      )}

      <Button
        variant="ghost"
        size="sm"
        disabled={!hasMore}
        onClick={() => go(page + 1)}
        aria-label="Next page"
      >
        <ChevronRight className="size-4" />
      </Button>
    </nav>
  );
}
