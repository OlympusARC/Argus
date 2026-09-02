import { Suspense } from "react";

import { Filters } from "@/components/filters";
import { Header } from "@/components/header";
import { JobTable } from "@/components/job-table";
import { Pager } from "@/components/pager";
import { StatBar } from "@/components/stats";
import { Skeleton } from "@/components/ui/skeleton";
import { countJobs, getStats, listJobs, type Filters as F } from "@/lib/jobs";

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
    page: Number(one("page") ?? 1) || 1,
  };

  const [{ jobs, hasMore }, stats, matching] = await Promise.all([
    listJobs(filters),
    getStats(),
    countJobs(filters),
  ]);

  return (
    <>
      <Header />
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 pt-24 pb-10 sm:px-8 lg:pt-28 lg:pb-14">
        <header className="flex flex-col gap-8">
          <div>
            {/*
              The name lives in the header. Repeating it here as an h1 put the
              word twice in the first 80 pixels of the page, so the heading is
              the sentence that actually says what this is.
            */}
            <h1 className="max-w-prose text-sm text-muted-foreground">
              Open engineering, product and adjacent roles, aggregated from nine applicant
              tracking systems and refreshed hourly.
            </h1>
          </div>
          <StatBar stats={stats} />
        </header>

        <hr className="my-8 border-border" />

        <Suspense fallback={<Skeleton className="h-9 w-full max-w-3xl" />}>
          <Filters total={matching ?? stats.total} filtered={matching !== null} />
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
