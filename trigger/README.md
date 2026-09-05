# argus-trigger

Fires Argus's scheduled workflows, because GitHub's scheduler does not.

Measured on `0 * * * *`: seven poll runs in twenty-four hours against
twenty-four scheduled, with gaps of 2.4 to 5.0 hours. GitHub documents this --
the schedule event is delayed under load, "high load times include the start
of every hour", and queued jobs may be dropped.

| cron (UTC)     | workflow        |
| -------------- | --------------- |
| `37 * * * *`   | poll.yml        |
| `43 3 * * *`   | discover.yml    |
| `17 9 * * *`   | orchestrate.yml |

Three of the five cron triggers a free Cloudflare account gets. The 10ms CPU
limit is not binding: Cloudflare bills CPU, not wall clock, so waiting on
GitHub's response costs nothing.

Each workflow keeps a **weekly** `schedule:` entry of its own. That is what a
dead Worker degrades to -- a trigger that stops firing raises no error
anywhere, it just quietly stops, so the floor matters more than its cadence.

## Deploy

    npm install -g wrangler
    wrangler login
    wrangler secret put GITHUB_TOKEN     # paste the PAT; it never touches a file
    wrangler deploy

The token is a fine-grained PAT scoped to `OlympusARC/Argus` alone, with
**Actions: read and write** and nothing else.

## Check it

    curl https://argus-trigger.<subdomain>.workers.dev

Reports the last run of each workflow rather than dispatching one, so anything
can poll it without side effects.

    wrangler tail        # live logs, including a failed dispatch
