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
  "workable",
  "bamboohr",
  "breezy",
  "recruitee",
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

export const DEFAULT_SORT: SortKey = "seen";
export const DEFAULT_DIR = "desc";

export function parseSort(sort?: string, dir?: string) {
  const key: SortKey = sort && sort in SORTS ? (sort as SortKey) : DEFAULT_SORT;
  return { key, desc: dir !== "asc" };
}
