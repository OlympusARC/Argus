/**
 * Fire Argus's scheduled workflows, because GitHub's own scheduler does not.
 *
 * Measured on `0 * * * *`: seven poll runs in twenty-four hours against
 * twenty-four scheduled, with gaps of 2.4 to 5.0 hours. GitHub's
 * documentation predicts exactly that -- the schedule event is delayed under
 * load, "high load times include the start of every hour", and "if the load
 * is sufficiently high enough, some queued jobs may be dropped".
 *
 * Every workflow keeps a sparser `schedule:` entry of its own as a backstop,
 * so a dead Worker degrades to a few runs a day rather than to silence. That
 * is the failure worth designing for: a trigger that stops firing produces no
 * error anywhere, it just quietly stops.
 *
 * None of these run at :00, for the contention reason above and so they never
 * queue behind each other -- poll and orchestrate share a concurrency group.
 */
const OWNER = "OlympusARC";
const REPO = "Argus";

const SCHEDULE = {
  "37 * * * *": "poll.yml",
  "43 3 * * *": "discover.yml",
  "17 9 * * *": "orchestrate.yml",
};

export default {
  async scheduled(event, env, ctx) {
    const workflow = SCHEDULE[event.cron];
    /**
     * An unrecognised cron means wrangler.toml and this map disagree, which
     * would otherwise be a workflow that silently never runs.
     */
    if (!workflow) {
      console.error(`no workflow mapped to cron ${event.cron}`);
      return;
    }
    ctx.waitUntil(dispatch(env, workflow));
  },

  /**
   * A GET reports the last run of each workflow, so the trigger can be
   * checked without waiting for its next fire. It never dispatches -- a URL
   * with a side effect is one a crawler can pull.
   */
  async fetch(request, env) {
    const out = {};
    for (const workflow of Object.values(SCHEDULE)) {
      const r = await gh(env, `/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/runs?per_page=1`);
      if (!r.ok) {
        out[workflow] = { error: `github ${r.status}` };
        continue;
      }
      const run = (await r.json()).workflow_runs?.[0];
      out[workflow] = run && {
        started: run.run_started_at,
        event: run.event,
        status: run.status,
        conclusion: run.conclusion,
      };
    }
    return Response.json({ ok: true, workflows: out });
  },
};

function gh(env, path, init = {}) {
  return fetch(`https://api.github.com${path}`, {
    ...init,
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "Content-Type": "application/json",
      // GitHub rejects an API request that sends no User-Agent.
      "User-Agent": "argus-trigger",
      ...(init.headers || {}),
    },
  });
}

async function dispatch(env, workflow) {
  const r = await gh(env, `/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`, {
    method: "POST",
    body: JSON.stringify({ ref: "main" }),
  });
  /**
   * A successful dispatch is 204 with no body. Logging the failure body is
   * the only way to tell a revoked token from a renamed workflow, and either
   * one otherwise just stops the workflow running with no error anywhere.
   */
  if (r.status !== 204) {
    console.error(`${workflow}: dispatch failed ${r.status} ${await r.text()}`);
  } else {
    console.log(`${workflow}: dispatched`);
  }
}
