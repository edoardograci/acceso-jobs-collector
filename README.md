# Acceso jobs collector

Manual, bounded LinkedIn discovery for the Acceso admin. Source requests, parsing and logo normalization run on GitHub Actions. No source account, paid API, proxy, CAPTCHA solver or automatic schedule.

## Run manually

In Actions → Collect LinkedIn jobs → Run workflow, choose:

- `search`: worldwide keyword discovery, rotating across saved query/page checkpoints.
- `companies`: company-filtered public job search. Optionally choose an active watchlist company slug.
- `verify`: recheck retained jobs whose successful verification is at least seven days old.
- `maintenance`: apply retention and cache eligible existing logos without making LinkedIn Jobs requests.

Start with one page, three companies and five candidates. Enable delivery to update the admin, or leave it off for a dry run. **Both modes acquire the shared lease and respect cooldowns.** Browser fallback is off by default. Public company searches supplement keyword coverage; they are not a guaranteed independent company feed.

## Connection and privacy

The only required variable is `JOBS_INGEST_URL=https://acceso.design/api/jobs-collector`. Encrypted Actions secret `JOBS_INGEST_TOKEN` matches the website server secret. The collector starts an authenticated session that returns the active watchlist, compact known-job metadata, suppressions and durable progress. It has no R2 credentials and receives no private review notes or full stored descriptions.

Public R2 operational exports are no longer a state source. `JOBS_WATCHLIST_URL` is supported only for standalone local runs without a connected session. Review decisions, collection progress and cooldowns live in the website's private R2 bucket. The public repository contains code and a bootstrap map of public company IDs, never credentials or private workspace data.

## Reliability and bounds

- One durable lease, renewed at every page checkpoint. Lost sessions expire after ten minutes.
- A block or HTTP 429 stops the run and starts a shared six-hour cooldown. Failed runs cool down for fifteen minutes; normal runs for one minute. A new workflow cannot bypass this by restarting.
- Four seconds between Jobs requests, an 80-fetch default budget, a 35-minute source budget and a 50-minute workflow limit.
- Each accepted page saves incremental candidates, observations and the cursor atomically with a unique delivery ID. Retried deliveries do not duplicate jobs. A candidate cap preserves the current page; successfully processed pages advance.
- Keywords and companies rotate by last attempt. Failed scopes get a one-day retry delay; successful coverage is recorded separately from attempts.
- Source searches cover a 90-day horizon. Public eligibility expires 90 days after original posting, or first discovery if the date is unknown. Rediscovery does not reset age.
- A small verification pass runs before discovery. Confirmed closure hides a listing; a failed fetch only records unavailable verification. Jobs unverified for fourteen days are excluded from the public feed.
- Complete known records skip description requests until verification is due. Old full-detail caches are never treated as fresh verification.
- Source descriptions are cleaned and capped at 12,000 characters with an explicit truncation flag. External application URLs are preferred; otherwise the canonical LinkedIn job is retained.
- Logos are fetched only from the LinkedIn CDN, without redirects, with byte/pixel limits. At most ten employer logos per run are normalized to metadata-free WebP, uploaded through the authenticated endpoint and deduplicated by content hash in Acceso's R2 bucket. Public jobs use cached URLs or a fallback, never expiring LinkedIn image URLs.

A completed GitHub workflow may still have `partial` coverage when a budget was reached. The summary separates coverage notes from source errors. `blocked` and `failed` outcomes fail the workflow explicitly. No implementation guarantees exhaustive LinkedIn coverage or uninterrupted public access.

## Recovery

One-day artifacts include `result.json` (the latest idempotent checkpoint) and, on a completed run, `full-result.json` (all candidates from that run). Source payloads contain no review decisions. An interrupted run can be recovered through the authenticated admin import. Never replay an expired lease directly; start a new session to resume its durable cursor.

## Local tests

Python 3.12:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -p "test_*.py" -v
python scraper.py --mode search --watchlist-url https://json.acceso.design/jobs/watchlist.json --pages 1 --max-jobs 5 --browser never
```

Core dependencies and workflow actions are pinned. Install `requirements-browser.txt` and fetch Camoufox only when explicitly using the optional public browser transport. Free runner compute for a public repository does not mean unlimited source requests, artifact storage or service allowances.
# Pre-launch validation, 3 November 2026

The four-keyword live test returned 66 candidates from 11 result pages, then
LinkedIn returned HTTP 429 on request 78. Checkpoints were delivered before the
stop. This is partial coverage, not proof of reliable unrestricted collection.
Do not rerun during shared cooldown or interpret the cooldown duration as a
LinkedIn guarantee. Public workflow defaults now use one page per keyword and a
20-request total budget (including job details and verification). These are
conservative starting limits, not a proven safe threshold. Oldest-attempted
queries rotate first on subsequent manual runs. No schedule is enabled.

Descriptions preserve paragraphs, inline words and list items as plain text;
the original extraction is sent separately for private editorial comparison.
The collector never sends the website URL or ingestion credentials to LinkedIn.
