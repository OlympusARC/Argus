# Argus dashboard

A read-only view of the corpus: open engineering, product and adjacent roles,
filterable by family, level, source, title and location.

```bash
npm install
npm run dev          # http://localhost:3000
```

Needs one variable, in `web/.env.local`:

```
DATABASE_URL=postgresql://…@aws-0-<region>.pooler.supabase.com:6543/postgres
```

Port **6543**, not 5432. That is Supabase's transaction pooler, and a
dashboard opens many short connections where the pipeline holds one long one
— the session pooler runs out of slots long before the transaction pooler
notices.

## How it is put together

Server components query Postgres directly. There is a FastAPI read surface in
`../api`, but pointing the dashboard at it would mean running and deploying
two services to render one table.

Filter state lives in the URL rather than in React. That keeps the table a
server component — no job data is shipped to the browser to be filtered there
— and it makes every view linkable, which is most of what a dashboard is for.

## Notes that are easy to get wrong

- **`searchParams` is a Promise** in Next 16 and must be awaited. Reading it
  directly yields undefined filters and a silently unfiltered page.
- **Constants shared with a client component cannot live beside the queries.**
  The filter bar needs the family and level lists; when those sat in
  `lib/jobs.ts`, importing them pulled `pg` into the browser bundle and the
  build failed. They live in `lib/taxonomy.ts`, which imports nothing.
  `lib/db.ts` imports `server-only` so the next attempt fails loudly.
- **`line-clamp` does not constrain a table cell.** The cell grows to fit its
  longest content and drags the row past the viewport. `table-fixed` plus
  `truncate` is what actually holds the column widths.
- **The unfiltered count is not worth a query.** `COUNT(*)` over 127k rows is
  a sequential scan to print a number the stat bar already shows, so it only
  runs when a filter is set — which is exactly when it stops being obvious.
- **Arrivals are not a useful figure yet.** The corpus was last rebuilt in one
  pass, so every posting shares a `first_seen_at` inside the same day and
  "new today" reads as the whole table. The stat bar shows active boards
  instead until hourly polling has been running longer than a day.
