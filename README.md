# Acceso jobs collector

Manual LinkedIn job discovery for the Acceso admin workspace. Scraping and HTML parsing run on GitHub Actions, independently of the website CPU. No paid API, login credentials, cookies, proxies, CAPTCHA bypass, or scheduled runs.

## Two collection modes

- **search** (primary): public LinkedIn Jobs search filtered by keywords and optional location. Employers do not have to be on the watchlist. Default keywords: industrial designer, CMF designer, footwear designer, structural packaging designer.
- **companies** (extra): company-filtered public LinkedIn vacancies for active watchlist employers. Small batches rotate by least-recently checked employer using the Actions cache. Company searches supplement the main search; they do not establish exhaustive coverage.

## Manual run

Open **Actions → Collect LinkedIn jobs → Run workflow**. Start with one page, 12 jobs and three companies. Keep **Send results to the admin review workspace** off for a dry run. Download the `collection-result` artifact and inspect `result.json`; the workflow summary reports counts, missing details and coverage warnings. Results are retained for one day. The repository contains no collected jobs, employer watchlist or credentials.

Repository variables `JOBS_WATCHLIST_URL` and `JOBS_STATE_URL` point to the public R2 JSON exports. For automatic delivery later, set variable `JOBS_INGEST_URL` to the website's `/api/jobs-collector` endpoint and encrypted Actions secret `JOBS_INGEST_TOKEN` to the same random secret in the website's server settings. Do not put the secret in source code, variables, public JSON or logs. The collector needs no R2 access keys and cannot approve jobs or edit the watchlist.

## Limits and behavior

One run at a time; a 50-minute workflow timeout; a 40-minute collection budget; at least four seconds between requests. Requests stop without retry on HTTP 429. Public browser fallback is optional and stops at login/challenge pages. Cached complete company-job details avoid repeat fetching. Approved/discarded or complete known records skip detail requests. Incomplete records remain eligible for enrichment. Descriptions are capped at 12,000 characters. Company logos are LinkedIn CDN URLs and may expire. Missing fields are retained for review, never invented. Failed fetches never remove saved jobs.

Source access may vary by IP address and time. Local success does not guarantee GitHub-hosted access. Partial coverage and blocks are reported in run errors. There is no guarantee of complete LinkedIn coverage or unlimited source requests. Standard GitHub-hosted runners in public repositories follow GitHub's public-repository billing allowances; caches, artifacts and storage still have limits.

## Local checks

Python 3.12:

```sh
python -m pip install -r requirements.txt
python -m unittest test_scraper.py -v
python scraper.py --mode search --watchlist-url https://json.acceso.design/jobs/watchlist.json --pages 1 --max-jobs 12 --browser never
```

For the optional public browser fallback, install it with `python -m camoufox fetch` (Linux also needs `python -m playwright install-deps firefox`). Collection only uploads with `--submit`. The admin remains the source of truth for review decisions.
