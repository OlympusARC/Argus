import { Suspense } from "react";

import { Filters } from "@/components/filters";
import { Header } from "@/components/header";
import { JobTable } from "@/components/job-table";
import { Pager } from "@/components/pager";
import { Skeleton } from "@/components/ui/skeleton";
import { countJobs, listJobs, type Filters as F } from "@/lib/jobs";

export const dynamic = "force-dynamic";

/**
 * searchParams is a Promise in Next 16 and has to be awaited before it can be
 * read -- passing it straight to a query silently yields undefined filters.
 */
export default async function Page({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const sp = await searchParams;
  const one = (k: string) => {
    const v = sp[k];
    return Array.isArray(v) ? v[0] : v;
  };

  const filters: F = {
    q: one("q"),
    family: one("family"),
    seniority: one("seniority"),
    ats: one("ats"),
    sort: one("sort"),
    dir: one("dir"),
    page: Number(one("page") ?? 1) || 1,
  };

  const [{ jobs, hasMore }, count] = await Promise.all([listJobs(filters), countJobs(filters)]);

  return (
    <>
      <Header />
      <main className="w-full flex-1 px-4 pt-24 pb-10 sm:px-6 lg:pt-28 lg:pb-14">
        <Suspense fallback={<Skeleton className="h-9 w-full max-w-2xl" />}>
          <Filters total={count} />
        </Suspense>

        <div className="mt-5 flex flex-col gap-5">
          <JobTable jobs={jobs} />
          <Suspense>
            <Pager page={filters.page ?? 1} hasMore={hasMore} />
          </Suspense>
        </div>

        <footer className="mt-14 text-xs text-muted-foreground">
          Filtered at ingest: six role types, and locations not positively outside the US
          or Europe. Posted dates come from the board; Workday and BambooHR publish none.
        </footer>
      </main>
    </>
  );
}
