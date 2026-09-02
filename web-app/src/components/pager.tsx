"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";

export function Pager({ page, hasMore }: { page: number; hasMore: boolean }) {
  const router = useRouter();
  const params = useSearchParams();

  const go = (n: number) => {
    const next = new URLSearchParams(params.toString());
    if (n <= 1) next.delete("page");
    else next.set("page", String(n));
    router.push(`/?${next.toString()}`, { scroll: false });
  };

  if (page === 1 && !hasMore) return null;

  return (
    <div className="flex items-center justify-between">
      <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => go(page - 1)}>
        <ChevronLeft className="size-4" />
        Previous
      </Button>
      <span className="text-xs text-muted-foreground tabular-nums">Page {page}</span>
      <Button variant="outline" size="sm" disabled={!hasMore} onClick={() => go(page + 1)}>
        Next
        <ChevronRight className="size-4" />
      </Button>
    </div>
  );
}
