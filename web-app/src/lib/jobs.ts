import "server-only";

import { query } from "./db";
import { PAGE_SIZE, SORTS, parseSort, type Filters, type Job } from "./taxonomy";

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

  /**
   * Company matches the name or the board slug, because the table shows the
   * slug whenever the name is unknown -- 6,382 companies have no resolved
   * name yet, and searching only c.name would silently miss every row the
   * reader can see a value in.
   */
  /**
   * A stored column, so this is an index seek rather than a gazetteer run
   * over every candidate row. Several checked regions are an OR, and none
   * checked means no constraint -- not zero results.
   */
  if (f.regions?.length) {
    const start = args.length + 1;
    args.push(...f.regions);
    const marks = f.regions.map((_, i) => `$${start + i}`).join(", ");
    parts.push(`j.region IN (${marks})`);
  }

  if (f.company) {
    const like = `%${f.company}%`;
    args.push(like, like);
    parts.push(`(c.name ILIKE $${args.length - 1} OR j.slug ILIKE $${args.length})`);
  }

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
  /**
   * The same joins as the listing, because the company filter and the
   * company sort both reach through them. Counting from jobs alone was fine
   * while every filter was a column on jobs; it would now be a missing-column
   * error the moment someone searched a company.
   */
  const [row] = await query<{ n: string }>(
    `SELECT COUNT(*) n
       FROM jobs j
       LEFT JOIN boards b ON b.ats = j.ats AND b.slug = j.slug
       LEFT JOIN companies c ON c.id = b.company_id
      WHERE ${sql}`,
    args,
  );
  return Number(row.n);
}

export async function listJobs(f: Filters): Promise<{ jobs: Job[]; hasMore: boolean }> {
  const { sql, args } = where(f);
  const page = Math.max(0, (f.page ?? 1) - 1);

  /**
   * The sort key comes from SORTS, never from the query string. A column
   * name is part of the statement rather than a value, so it cannot be
   * parameterised -- the allowlist is the whole defence.
   *
   * NULLS LAST in both directions on purpose: 55,172 open roles have no
   * posted date because Workday and BambooHR publish none, and sorting
   * ascending should not open with 55,172 blanks. The secondary key keeps
   * the order stable across pages when the primary ties.
   */
  const { key, desc } = parseSort(f.sort, f.dir);
  const column = SORTS[key];
  const direction = desc ? "DESC" : "ASC";

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
      ORDER BY ${column} ${direction} NULLS LAST, j.first_seen_at DESC
      LIMIT $${args.length + 1} OFFSET $${args.length + 2}`,
    [...args, PAGE_SIZE + 1, page * PAGE_SIZE],
  );

  return { jobs: rows.slice(0, PAGE_SIZE), hasMore: rows.length > PAGE_SIZE };
}
