/**
 * Shared vocabulary, and deliberately free of any database import.
 *
 * The filter bar is a client component and needs these lists. When they lived
 * beside the queries, importing them pulled `pg` into the browser bundle and
 * the build failed with a wall of module-not-found. Constants that both sides
 * need belong in a module that imports nothing.
 */
export const FAMILIES = ["engineering", "fde", "ai", "data", "security", "product"] as const;

export const SENIORITIES = [
  "intern",
  "new_grad",
  "senior",
  "staff",
  "lead",
  "principal",
  "manager",
  "director",
  "executive",
] as const;

export const ATSES = [
  "workday",
  "greenhouse",
  "smartrecruiters",
  "ashby",
  "lever",
  "bamboohr",
  "breezy",
  "recruitee",
] as const;

/**
 * Regions, as the dashboard offers them. `other` is absent on purpose: the
 * ingest filter refuses anything positively outside the US and Europe, so
 * the 187 rows carrying it predate that filter and are not worth a control.
 *
 * `unknown` is offered, and matters -- 23,878 postings name a place no
 * gazetteer here recognises, or name none at all, and most Workday postings
 * are among them. Hiding them behind no control would quietly remove a
 * quarter of the corpus from every filtered view.
 */
export const REGIONS = [
  { value: "us", label: "United States" },
  { value: "europe", label: "Europe" },
  { value: "remote", label: "Remote" },
  { value: "unknown", label: "Unspecified" },
] as const;

export const PAGE_SIZE = 50;

export type Job = {
  ats: string;
  slug: string;
  external_id: string;
  title: string;
  url: string | null;
  location: string | null;
  role_family: string | null;
  seniority: string | null;
  is_fde: boolean | null;
  posted_at: number | null;
  first_seen_at: number | null;
  company: string | null;
};

export type Filters = {
  q?: string;
  company?: string;
  regions?: string[];
  family?: string;
  seniority?: string;
  ats?: string;
  sort?: string;
  dir?: string;
  page?: number;
};

/**
 * Sortable columns, as an allowlist mapping a URL token to a column.
 *
 * A sort key cannot be parameterised -- it is part of the statement, not a
 * value -- so the only safe way to accept one from a query string is to
 * refuse anything not named here. Never interpolate the raw token.
 */
export const SORTS = {
  posted: "j.posted_at",
  seen: "j.first_seen_at",
  title: "j.title",
  company: "c.name",
} as const;

export type SortKey = keyof typeof SORTS;

/**
 * Newest posting first, which is what someone opening a job board wants.
 *
 * It sorts by what the board claims rather than by when this pipeline
 * noticed, and those are very different orderings right now: the corpus was
 * ingested in one pass, so first_seen_at is near-identical across 83,682
 * rows and sorting by it is close to arbitrary.
 *
 * The cost is that NULLS LAST puts every undated posting behind every dated
 * one, so page one is dated rows only. That is the right default -- an
 * undated posting cannot be shown as recent honestly -- and Seen remains one
 * click away for anyone who wants the other view.
 */
export const DEFAULT_SORT: SortKey = "posted";
export const DEFAULT_DIR = "desc";

export function parseSort(sort?: string, dir?: string) {
  const key: SortKey = sort && sort in SORTS ? (sort as SortKey) : DEFAULT_SORT;
  return { key, desc: dir !== "asc" };
}
