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

  return { sql: parts.join(" AND "), args };
}

/**
 * How many rows match, filtered or not.
 *
 * This was conditional while a stat bar showed the corpus size; with that
 * gone it is the only statement of how large the result is, so it always
 * runs. Measured at 128 ms unfiltered against 127k rows, and faster with a
 * filter, since then it rides the same index the listing does.
 */
export async function countJobs(f: Filters): Promise<number> {
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
            j.role_family, j.seniority, j.is_fde, j.posted_at, j.first_seen_at,
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
