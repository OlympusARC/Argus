import "server-only";

import { query } from "./db";
import { PAGE_SIZE, type Filters, type Job } from "./taxonomy";

export * from "./taxonomy";

/**
 * Every filter but the two text ones is an indexed column, which is the
 * difference between a seek and a scan of the whole corpus. The text filters
 * are ILIKE and are the slow ones by design -- they are also the ones a
 * person types deliberately rather than leaves set.
 */
function where(f: Filters): { sql: string; args: unknown[] } {
  const parts = ["j.status = 'open'"];
  const args: unknown[] = [];
  const add = (clause: string, value: unknown) => {
    args.push(value);
    parts.push(clause.replace("?", `$${args.length}`));
  };

  if (f.family) add("j.role_family = ?", f.family);
  if (f.seniority) add("j.seniority = ?", f.seniority);
  if (f.ats) add("j.ats = ?", f.ats);
  if (f.q) add("j.title ILIKE ?", `%${f.q}%`);
  if (f.location) add("j.location ILIKE ?", `%${f.location}%`);

  return { sql: parts.join(" AND "), args };
}

/**
 * The matching count, and only when something is filtered.
 *
 * An unfiltered COUNT(*) over 127k rows is a sequential scan to tell the
 * reader a number the stat bar already shows. With a filter applied the
 * count rides the same index the listing does, so it is cheap -- and that is
 * exactly when the number stops being obvious.
 */
export async function countJobs(f: Filters): Promise<number | null> {
  const hasFilter = Boolean(f.family || f.seniority || f.ats || f.q || f.location);
  if (!hasFilter) return null;
  const { sql, args } = where(f);
  const [row] = await query<{ n: string }>(
    `SELECT COUNT(*) n FROM jobs j WHERE ${sql}`,
    args,
  );
  return Number(row.n);
}

export async function listJobs(f: Filters): Promise<{ jobs: Job[]; hasMore: boolean }> {
  const { sql, args } = where(f);
  const page = Math.max(0, (f.page ?? 1) - 1);

  /**
   * One row over the page size, so "is there a next page" costs nothing.
   * A COUNT(*) over a filtered 127k-row table costs a scan and is only ever
   * used to grey out a button.
   */
  const rows = await query<Job>(
    `SELECT j.ats, j.slug, j.external_id, j.title, j.url, j.location,
            j.role_family, j.seniority, j.is_fde, j.first_seen_at,
            c.name AS company
       FROM jobs j
       LEFT JOIN boards b ON b.ats = j.ats AND b.slug = j.slug
       LEFT JOIN companies c ON c.id = b.company_id
      WHERE ${sql}
      ORDER BY j.first_seen_at DESC NULLS LAST
      LIMIT $${args.length + 1} OFFSET $${args.length + 2}`,
    [...args, PAGE_SIZE + 1, page * PAGE_SIZE],
  );

  return { jobs: rows.slice(0, PAGE_SIZE), hasMore: rows.length > PAGE_SIZE };
}

export type Stats = {
  total: number;
  families: { role_family: string; n: number }[];
  boards: number;
  companies: number;
};

export async function getStats(): Promise<Stats> {
  /**
   * Boards rather than "new today". The corpus was last rebuilt in one pass,
   * so every posting shares a first_seen_at inside the same day and the
   * arrivals figure reads as the whole table -- true, and useless. It becomes
   * a real number once hourly polling has been running longer than a day.
   */
  const [[total], families, [boards], [companies]] = await Promise.all([
    query<{ n: string }>("SELECT COUNT(*) n FROM jobs WHERE status = 'open'"),
    query<{ role_family: string; n: string }>(
      `SELECT role_family, COUNT(*) n FROM jobs WHERE status = 'open'
        GROUP BY role_family ORDER BY n DESC`,
    ),
    query<{ n: string }>("SELECT COUNT(*) n FROM boards WHERE status = 'active'"),
    query<{ n: string }>("SELECT COUNT(*) n FROM companies"),
  ]);

  return {
    total: Number(total.n),
    boards: Number(boards.n),
    companies: Number(companies.n),
    families: families.map((r) => ({ role_family: r.role_family, n: Number(r.n) })),
  };
}
