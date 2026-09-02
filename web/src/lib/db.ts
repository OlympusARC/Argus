import "server-only";

import { Pool } from "pg";

/**
 * One pool per process, cached across hot reloads.
 *
 * The connection string points at Supabase's transaction pooler on 6543
 * rather than the session pooler on 5432. A dashboard opens many short
 * connections where a poller holds one long one, and the session pooler runs
 * out of slots long before the transaction pooler notices.
 */
declare global {
  var __argusPool: Pool | undefined;
}

export const pool =
  global.__argusPool ??
  new Pool({
    connectionString: process.env.DATABASE_URL,
    max: 4,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 10_000,
  });

if (process.env.NODE_ENV !== "production") global.__argusPool = pool;

export async function query<T>(sql: string, params: unknown[] = []): Promise<T[]> {
  const res = await pool.query(sql, params);
  return res.rows as T[];
}
