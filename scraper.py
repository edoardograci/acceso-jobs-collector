"""Collect public LinkedIn company vacancies. No login, paid API, AI or proxies.

R2/admin remains the source of truth for decisions. This collector sends raw
candidates; the admin service validates, deduplicates and routes them.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
import uuid

from bs4 import BeautifulSoup
import requests

ROOT = Path(__file__).resolve().parent
DESIGN_SIGNAL = re.compile(r"design|\bcmf\b|sculpt|modeller|modeler|modelmak|footwear|packaging|\bcad\b", re.I)
SEARCH_TERMS = 'designer OR "industrial design" OR CMF OR sculptor OR modeler OR modeller OR modelmaker OR CAD OR packaging OR footwear'
DEFAULT_KEYWORDS = ['industrial designer', 'CMF designer', 'footwear designer', 'structural packaging designer']
COMPANY_PATH = re.compile(r"^/company/([a-z0-9_-]+)(?:/(?:jobs|about|posts))?/?$", re.I)


class CollectionError(RuntimeError):
    pass


class RateLimited(CollectionError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def company_url(value):
    parsed = urlparse(str(value))
    match = COMPANY_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or not re.fullmatch(r"(?:[a-z]{2,3}\.)?linkedin\.com", parsed.hostname or "", re.I) or parsed.username or parsed.password or parsed.port or not match:
        raise CollectionError("Expected an HTTPS LinkedIn company URL")
    return f"https://www.linkedin.com/company/{match[1].lower()}/"


def companies_from(data):
    if data.get("version") != 1 or not isinstance(data.get("companies"), list) or len(data["companies"]) > 2000:
        raise CollectionError("Invalid watchlist format")
    companies = []
    seen = set()
    for row in data["companies"]:
        url = company_url(row.get("url"))
        cid = url.split("/")[4]
        if cid in seen:
            continue
        seen.add(cid)
        name = str(row.get("name", "")).strip()[:160]
        numeric = str(row.get("linkedinId") or "")
        if not name or (numeric and not re.fullmatch(r"\d{1,20}", numeric)):
            raise CollectionError(f"Invalid company name or numeric ID: {cid}")
        if row.get("enabled", True):
            companies.append({"id": cid, "name": name, "url": url, "linkedinId": numeric})
    return companies


def wall(html):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    # Public pages can include an optional sign-in form alongside real content.
    # Its presence alone must not turn a successful public fetch into a failure.
    return bool(soup.select_one("#challenge-running, #captcha-internal, form[action*='checkpoint']")) or any(x in title for x in ("just a moment", "security verification", "authwall", "sign in | linkedin", "log in or sign up"))


class Fetcher:
    def __init__(self, browser="auto", pause=2.0):
        self.mode, self.pause = browser, pause
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"})
        self.context = self.browser = self.page = None
        self.browser_error = None

    def get(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "www.linkedin.com":
            raise CollectionError("Collector only fetches www.linkedin.com")
        time.sleep(self.pause)
        if self.mode != "always":
            response = None
            try:
                response = self.session.get(url, timeout=20, allow_redirects=True)
            except requests.RequestException as error:
                if self.mode == 'never':
                    reason = 'Local network permission denied' if '10013' in str(error) or 'Permission denied' in str(error) else 'LinkedIn HTTP connection failed or timed out'
                    raise CollectionError(reason) from None
                # A transport failure must still reach the browser fallback.
            if response is not None and response.status_code == 429:
                raise RateLimited("LinkedIn rate limited this check; remaining companies deferred without retry")
            if response is not None and response.ok and not wall(response.text) and "authwall" not in response.url and "/login" not in response.url:
                return response.text
            if response is not None and response.status_code == 404:
                raise CollectionError("LinkedIn page unavailable (404)")
            if self.mode == "never":
                raise CollectionError(f"LinkedIn returned a challenge, login wall or HTTP {response.status_code}")
        return self._browser_get(url)

    def _browser_get(self, url):
        if self.browser is None:
            if self.browser_error:
                raise CollectionError(self.browser_error)
            try:
                from camoufox.sync_api import Camoufox
                self.context = Camoufox(headless=True)
                self.browser = self.context.__enter__()
                self.page = self.browser.new_page(locale="en-US")
            except Exception as error:
                print(f"Camoufox startup diagnostic: {error}", file=sys.stderr)
                self.browser_error = "Camoufox unavailable; install requirements and run python -m camoufox fetch"
                raise CollectionError(self.browser_error) from None
        try:
            response = self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self.page.wait_for_timeout(2000)
            html = self.page.content()
            if response and response.status == 429:
                raise RateLimited('LinkedIn rate limited browser access; collection stopped without retry')
            if not response or response.status >= 400 or wall(html) or any(s in self.page.url for s in ("/authwall", "/checkpoint", "/login")):
                raise CollectionError("Browser encountered a challenge, login wall or unavailable page")
            return html
        except CollectionError:
            raise
        except Exception:
            raise CollectionError("Browser could not load this LinkedIn page") from None

    def close(self):
        context, self.context = self.context, None
        self.browser = self.page = None
        try:
            if context:
                context.__exit__(None, None, None)
        finally:
            self.session.close()


def organization_id(html):
    for pattern in (r'"companyId"\s*:\s*"?(\d+)', r'urn:li:(?:organization|fsd_company|fs_normalized_company):(\d+)', r'[?&]f_C=(\d+)'):
        match = re.search(pattern, html)
        if match:
            return match[1]
    raise CollectionError("Company numeric ID was not visible. Add its LinkedIn company ID in the watchlist, or retry later.")


def job_cards(html):
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for link in soup.select("a.base-card__full-link, a[href*='/jobs/view/']"):
        href = link.get("href", "")
        match = re.search(r"/jobs/view/(?:[^/?#]*-)?(\d+)(?:[/?#]|$)", href)
        if not match:
            continue
        card = link.find_parent("div", class_=re.compile("base-search-card|base-card")) or link.find_parent("li") or link
        title = card.select_one(".base-search-card__title, h3")
        employer = card.select_one(".base-search-card__subtitle, h4")
        location = card.select_one(".job-search-card__location")
        date = card.select_one("time[datetime]")
        employer_link = employer.select_one('a[href]') if employer else None
        slug = ''
        if employer_link:
            try: slug = company_url(employer_link['href']).split('/')[4]
            except CollectionError: pass
        logo = card.select_one('img')
        logo_url = (logo.get('data-delayed-url') or logo.get('src') or '') if logo else ''
        if not re.fullmatch(r'(?:[a-z0-9-]+\.)*licdn\.com', urlparse(logo_url).hostname or '', re.I): logo_url = ''
        result[match[1]] = {"id": match[1], "title": title.get_text(" ", strip=True) if title else link.get_text(" ", strip=True), "company": employer.get_text(" ", strip=True) if employer else "", "companyId": slug, "companyUrl": f'https://www.linkedin.com/company/{slug}/' if slug else '', "companyLogo": logo_url, "location": location.get_text(" ", strip=True) if location else "", "postedAt": date.get("datetime", "") if date else ""}
    return list(result.values())


def application_url(soup, fallback):
    link = soup.select_one("a.apply-button, a.topcard__link--apply, a[data-tracking-control-name*='apply-link']")
    # LinkedIn public details sometimes store the external destination in a hidden element.
    hidden = soup.select_one("#applyUrl")
    value = link.get("href", "") if link else hidden.get_text(strip=True) if hidden else ""
    value = value.strip().strip('"')
    if value.startswith("/"):
        value = urljoin("https://www.linkedin.com", value)
    query = parse_qs(urlparse(value).query)
    value = query.get("url", [value])[0]
    parsed = urlparse(value)
    return value if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password else fallback


def parse_detail(html, card, company):
    soup = BeautifulSoup(html, "html.parser")
    markup = soup.select_one(".show-more-less-html__markup, .description__text")
    if not markup:
        raise CollectionError("Job description was not accessible")
    title = soup.select_one(".top-card-layout__title, h1")
    employer = soup.select_one(".topcard__org-name-link")
    location = soup.select_one(".topcard__flavor--bullet")
    source = f"https://www.linkedin.com/jobs/view/{card['id']}/"
    description = markup.get_text("\n", strip=True)[:6000]
    if re.search(r"no longer accepting applications|this job is no longer available", soup.get_text(" ", strip=True), re.I):
        description = "No longer accepting applications.\n" + description
    slug = company.get('id') or card.get('companyId', '')
    if employer and employer.get('href'):
        try: slug = company_url(employer['href']).split('/')[4]
        except CollectionError: pass
    criteria = {}
    names = {'employment type':'employmentType', 'seniority level':'seniorityLevel', 'job function':'jobFunction', 'industries':'industries'}
    for row in soup.select('.description__job-criteria-item'):
        label = row.select_one('.description__job-criteria-subheader')
        value = row.select_one('.description__job-criteria-text')
        key = names.get(label.get_text(' ', strip=True).lower()) if label else None
        if key and value: criteria[key] = value.get_text(' ', strip=True)
    logo = soup.select_one('img.topcard__org-logo, img.artdeco-entity-image')
    logo_url = (logo.get('data-delayed-url') or logo.get('src') or '') if logo else ''
    if not re.fullmatch(r'(?:[a-z0-9-]+\.)*licdn\.com', urlparse(logo_url).hostname or '', re.I): logo_url = card.get('companyLogo', '')
    return {**card, **criteria, "companyLogo": logo_url, "companyUrl": f'https://www.linkedin.com/company/{slug}/' if slug else '', "detailComplete": True, "companyId": slug, "title": title.get_text(" ", strip=True) if title else card["title"], "company": employer.get_text(" ", strip=True) if employer else card["company"], "location": location.get_text(" ", strip=True) if location else card["location"], "sourceUrl": source, "applicationUrl": application_url(soup, source), "description": description}


def collect_search(watchlist, keywords=None, location='', browser='auto', pages=3, max_jobs=100, pause=2.0, fetcher=None, cache=None, known_ids=None):
    """Primary discovery is independent of employer membership in the watchlist."""
    keywords = keywords or DEFAULT_KEYWORDS
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 8 or any(not isinstance(k, str) or not k.strip() or len(k) > 100 for k in keywords):
        raise CollectionError('Supply 1–8 keywords of at most 100 characters each')
    companies = {c['id']: c for c in companies_from(watchlist)}
    disabled = {company_url(c['url']).split('/')[4] for c in watchlist['companies'] if c.get('enabled') is False}
    cache = cache if cache is not None else {'companies': {}, 'jobs': {}}
    known = set(known_ids or [])
    run = {'id': uuid.uuid4().hex, 'mode': 'search', 'startedAt': now(), 'finishedAt': '', 'checked': 0, 'found': 0, 'skippedKnown': 0, 'errors': []}
    items, seen = [], set()
    fetcher = fetcher or Fetcher(browser, pause)
    deadline = time.monotonic() + 40 * 60
    try:
        for keyword in keywords:
            for page in range(pages):
                if len(items) >= max_jobs or time.monotonic() > deadline:
                    run['errors'].append({'companyId': 'keyword-search', 'message': 'Candidate or time budget reached; search coverage is partial'})
                    return {'items': items, 'run': run}
                try:
                    query = {'keywords': keyword, 'location': location, 'start': page * 10, 'sortBy': 'DD', 'f_TPR': 'r2592000'}
                    html = fetcher.get('https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?' + urlencode(query))
                    cards = job_cards(html)
                    run['checked'] += 1
                    if not cards:
                        if html.strip() and not re.search(r'no (?:matching |results|jobs)|couldn.t find', html, re.I):
                            raise CollectionError('No recognizable results; coverage is inconclusive')
                        break
                    fresh = [c for c in cards if c['id'] not in seen]
                    if not fresh: break
                    for card in fresh:
                        seen.add(card['id'])
                        if card.get('companyId') in disabled or not DESIGN_SIGNAL.search(card['title']): continue
                        if card['id'] in known:
                            run['skippedKnown'] += 1
                            continue
                        if len(items) >= max_jobs: break
                        if time.monotonic() > deadline: raise CollectionError('Time budget reached; search coverage is partial')
                        company = companies.get(card.get('companyId'), {'id': card.get('companyId', '')})
                        try:
                            item = parse_detail(fetcher.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{card['id']}"), card, company)
                        except RateLimited: raise
                        except CollectionError as error:
                            item = {**card, 'detailComplete': False, 'description': '', 'applicationUrl': ''}
                            run['errors'].append({'companyId': card.get('companyId', ''), 'message': f"Job {card['id']}: {error}"})
                        item['collectionSource'] = 'search'
                        items.append(item)
                    if page == pages - 1:
                        run['errors'].append({'companyId': 'keyword-search', 'message': f'Pagination cap reached for {keyword}; coverage may be partial'})
                except RateLimited as error:
                    run['errors'].append({'companyId': 'keyword-search', 'message': str(error)[:300]})
                    return {'items': items, 'run': run}
                except CollectionError as error:
                    run['errors'].append({'companyId': 'keyword-search', 'message': str(error)[:300]})
                    break
    finally:
        fetcher.close()
        run['finishedAt'], run['found'] = now(), len(items)
    return {'items': items, 'run': run}


def collect(watchlist, max_companies=5, browser="auto", pages=3, max_jobs=300, pause=2.0, fetcher=None, cache=None, known_ids=None, shard=0, shards=1):
    cache = cache if cache is not None else {"companies": {}, "jobs": {}}
    cache.setdefault('companies', {}); cache.setdefault('jobs', {})
    known_ids = set(known_ids or [])
    import hashlib
    companies = [c for c in companies_from(watchlist) if int(hashlib.sha256(c['id'].encode()).hexdigest(),16) % shards == shard]
    companies.sort(key=lambda c: cache['companies'].get(c['id'],{}).get('lastChecked',''))
    companies = companies[:max_companies]
    run = {"id": uuid.uuid4().hex, "mode": "companies", "startedAt": now(), "finishedAt": "", "checked": 0, "found": 0, "skippedKnown": 0, "cachedDetails": 0, "errors": []}
    items, seen = [], set()
    fetcher = fetcher or Fetcher(browser, pause)
    deadline = time.monotonic() + 40 * 60
    try:
        for company in companies:
            if time.monotonic() > deadline:
                run["errors"].append({"companyId": company["id"], "message": "Run time budget reached; remaining companies deferred"})
                break
            if len(items) >= max_jobs:
                run["errors"].append({"companyId": company["id"], "message": "Run candidate limit reached; remaining companies deferred"})
                break
            run["checked"] += 1
            try:
                saved_company=cache['companies'].setdefault(company['id'],{})
                numeric = company["linkedinId"] or saved_company.get('numericId') or organization_id(fetcher.get(company["url"]))
                saved_company['numericId']=numeric
                page_signatures = set()
                for page in range(pages):
                    url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode({"f_C": numeric, "keywords": SEARCH_TERMS, "start": page * 10, "sortBy": "DD", "f_TPR": "r2592000"})
                    html = fetcher.get(url)
                    cards = job_cards(html)
                    signature = tuple(c['id'] for c in cards)
                    if signature and signature in page_signatures: break
                    page_signatures.add(signature)
                    if not cards and page == 0:
                        # Guest fragments can be empty even when the full public
                        # results page is available. Try that surface once.
                        html = fetcher.get('https://www.linkedin.com/jobs/search/?' + urlencode({"f_C": numeric, "keywords": SEARCH_TERMS, "sortBy": "DD", "f_TPR": "r2592000"}))
                        cards = job_cards(html)
                    if not cards:
                        fragment = re.sub(r"<!--.*?-->", "", html, flags=re.S).strip()
                        if page == 0 and fragment and not re.search(r"no (?:matching |results|jobs)|couldn.t find", html, re.I):
                            raise CollectionError("No recognizable results; this check is inconclusive")
                        break
                    for card in cards:
                        if time.monotonic() > deadline:
                            raise CollectionError("Run time budget reached; company coverage is partial")
                        if card["id"] in seen or not DESIGN_SIGNAL.search(card["title"]):
                            continue
                        seen.add(card["id"])
                        if card['id'] in known_ids:
                            run['skippedKnown'] += 1
                            continue
                        if len(items) >= max_jobs:
                            break
                        try:
                            if card['id'] in cache['jobs']:
                                item=cache['jobs'][card['id']]
                                run['cachedDetails'] += 1
                            else:
                                item = parse_detail(fetcher.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{card['id']}"), card, company)
                                cache['jobs'][card['id']]=item
                        except RateLimited:
                            raise
                        except CollectionError as error:
                            item = {**card, "detailComplete": False, "companyId": company["id"], "description": "", "applicationUrl": ""}
                            run["errors"].append({"companyId": company["id"], "message": f"Job {card['id']}: {error}"})
                        item = {**item, 'collectionSource': 'companies'}
                        items.append(item)
                    if len(items) >= max_jobs:
                        run["errors"].append({"companyId": company["id"], "message": "Candidate cap reached; company coverage may be partial"})
                        break
                    # Public search currently returns ten cards; a short page is
                    # not sufficient evidence that pagination is exhausted.
                    if page == pages - 1:
                        run["errors"].append({"companyId": company["id"], "message": "Pagination cap reached; company coverage is partial"})
            except RateLimited as error:
                run["errors"].append({"companyId": company["id"], "message": str(error)[:300]})
                break
            except CollectionError as error:
                run["errors"].append({"companyId": company["id"], "message": str(error)[:300]})
            finally:
                cache['companies'].setdefault(company['id'],{})['lastChecked']=now()
    finally:
        fetcher.close()
    run["finishedAt"], run["found"] = now(), len(items)
    return {"items": items, "run": run}


def load_cache(path):
    if not path.exists():return {'companies':{},'jobs':{}}
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value.get('companies'),dict) or not isinstance(value.get('jobs'),dict):
        raise CollectionError('Collector cache is invalid; preserve it for recovery before starting again.')
    return value


def save_cache(cache,path):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8')
    temporary.replace(path)


def known_jobs(state):
    # Incomplete candidates remain eligible for a successful detail fetch later.
    return {str(j['id']) for j in state.get('jobs',{}).get('items',[])+state.get('queue',{}).get('items',[])
            if j.get('status') in ('approved','discarded') or len(j.get('description',''))>=120 and j.get('applicationUrl') and j.get('postedAt')}


def save_result(result, directory=ROOT / "output"):
    directory.mkdir(parents=True, exist_ok=True)
    # Atomic local output; never overwrite the live R2 collection here.
    target = directory / "result.json"
    temp = directory / ("result-" + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return target


def submit_result(result, url, token):
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
        raise CollectionError("Ingestion endpoint must use HTTPS or local loopback")
    if not token or len(token) < 32:
        raise CollectionError("JOBS_INGEST_TOKEN must have at least 32 characters")
    # Origin is required by the site's existing request middleware. Bearer auth
    # is independently verified by the ingestion endpoint.
    origin = f"{parsed.scheme}://{parsed.netloc}"
    payload = json.dumps(result).encode("utf-8")
    if len(payload) > 5_000_000:
        raise CollectionError("Result exceeds ingestion limit; decrease max jobs")
    for attempt in range(3):
        try:
            response = requests.post(url, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Origin": origin}, timeout=60)
            if response.ok:
                return
            if response.status_code not in (409, 429, 502, 503):
                raise CollectionError(f"Results were not accepted (HTTP {response.status_code})")
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    raise CollectionError("Results could not be uploaded; result.json is retained for manual import")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watchlist", type=Path, default=ROOT / "data/watchlist.json")
    parser.add_argument("--watchlist-url", default=os.getenv("JOBS_WATCHLIST_URL"))
    parser.add_argument('--mode', choices=['search', 'companies'], default='search')
    parser.add_argument('--keywords-json', default=os.getenv('JOBS_KEYWORDS_JSON', ''))
    parser.add_argument('--location', default=os.getenv('JOBS_LOCATION', ''))
    parser.add_argument("--max-companies", type=int, default=5)
    parser.add_argument("--max-jobs", type=int, default=300)
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--browser", choices=["auto", "always", "never"], default="auto")
    parser.add_argument("--pause", type=float, default=2.0)
    parser.add_argument("--submit", action="store_true")
    parser.add_argument('--state-url',default=os.getenv('JOBS_STATE_URL'))
    parser.add_argument('--cache',type=Path,default=ROOT/'output/collector-cache.json')
    parser.add_argument('--shard',type=int,default=0)
    parser.add_argument('--shards',type=int,default=1)
    args = parser.parse_args()
    if not 1 <= args.max_companies <= 2000 or not 1 <= args.max_jobs <= 300 or not 1 <= args.pages <= 10 or args.pause < 1 or not 1<=args.shards<=16 or not 0<=args.shard<args.shards:
        parser.error("Limits: 1–2000 companies, 1–300 jobs, 1–10 pages, pause >= 1 second; valid shard required")
    if args.watchlist_url:
        response = requests.get(args.watchlist_url, timeout=30)
        response.raise_for_status()
        watchlist = response.json()
    else:
        watchlist = json.loads(args.watchlist.read_text(encoding="utf-8-sig"))
    if args.mode == 'companies' and not companies_from(watchlist):
        raise CollectionError("Watchlist has no active companies. Add companies in the admin panel first.")
    known=set()
    state_url=args.state_url or (urljoin(args.watchlist_url,'state.json') if args.watchlist_url else None)
    if state_url:
        response=requests.get(state_url,timeout=30);response.raise_for_status();known=known_jobs(response.json())
    cache=load_cache(args.cache)
    if args.mode == 'search':
        result = collect_search(watchlist, json.loads(args.keywords_json) if args.keywords_json else None, args.location, args.browser, args.pages, args.max_jobs, args.pause, cache=cache, known_ids=known)
    else:
        result = collect(watchlist, args.max_companies, args.browser, args.pages, args.max_jobs, args.pause, cache=cache,known_ids=known,shard=args.shard,shards=args.shards)
    request_id=os.getenv('JOBS_REQUEST_ID','')
    if request_id:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',request_id): raise CollectionError('Invalid admin request identifier')
        result['run']['id']=request_id
    path = save_result(result)
    save_cache(cache,args.cache)
    if args.submit:
        submit_result(result, os.getenv("JOBS_INGEST_URL", ""), os.getenv("JOBS_INGEST_TOKEN", ""))
    print(f"Mode {args.mode}: {result['run']['checked']} checks; {len(result['items'])} candidates; {len(result['run']['errors'])} failed/partial checks. Saved {path}")
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write(f"## {args.mode} collection\n\n{result['run']['checked']} checks; {len(result['items'])} candidates; {len(result['run']['errors'])} failed/partial checks.\n\n")
            for item in result['items'][:20]:
                stream.write(f"- {item['id']}: {item['title']} — {item['company']} ({len(item.get('description',''))} description characters; logo: {bool(item.get('companyLogo'))})\n")
            for error in result['run']['errors'][:20]: stream.write(f"- Coverage warning: {error['message']}\n")


if __name__ == "__main__":
    try:
        main()
    except (CollectionError, requests.RequestException, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
