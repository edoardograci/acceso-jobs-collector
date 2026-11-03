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

# Keep exception identities identical when the CLI imports the runtime engine.
if __name__ == '__main__':
    sys.modules['scraper'] = sys.modules[__name__]

ROOT = Path(__file__).resolve().parent
DESIGN_SIGNAL = re.compile(r"design|\bcmf\b|sculpt|modeller|modeler|modelmak|footwear|packaging|\bcad\b", re.I)
SEARCH_TERMS = 'designer OR "industrial design" OR CMF OR sculptor OR modeler OR modeller OR modelmaker OR CAD OR packaging OR footwear'
DEFAULT_KEYWORDS = ['industrial designer', 'CMF designer', 'footwear designer', 'structural packaging designer']
COMPANY_PATH = re.compile(r"^/company/([a-z0-9_-]+(?:%[0-9a-f]{2}[a-z0-9_-]*)*)(?:/(?:jobs|about|posts))?/?$", re.I)


class CollectionError(RuntimeError):
    pass


class RateLimited(CollectionError):
    pass

class AccessBlocked(CollectionError):
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
    def __init__(self, browser="auto", pause=4.0):
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
                response = self.session.get(url, timeout=20, allow_redirects=False)
            except requests.RequestException as error:
                if self.mode == 'never':
                    reason = 'Local network permission denied' if '10013' in str(error) or 'Permission denied' in str(error) else 'LinkedIn HTTP connection failed or timed out'
                    raise CollectionError(reason) from None
                # A transport failure must still reach the browser fallback.
            if response is not None and response.status_code == 429:
                raise RateLimited("LinkedIn rate limited this check; remaining companies deferred without retry")
            if response is not None and (response.status_code in (401,403,999) or 300 <= response.status_code < 400 or wall(response.text)):
                raise AccessBlocked('LinkedIn requires login or blocked public access; collection stopped without bypass')
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
                raise AccessBlocked("Browser encountered a challenge, login wall or unavailable page")
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
    for value in ([hidden.get_text(strip=True)] if hidden else []) + ([link.get('href','')] if link else []):
        value = value.strip().strip('"')
        if value.startswith('/'):
            value = urljoin('https://www.linkedin.com', value)
        value = parse_qs(urlparse(value).query).get('url', [value])[0]
        parsed = urlparse(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password: continue
        if re.fullmatch(r'(?:[a-z]{2,3}\.)?linkedin\.com', parsed.hostname, re.I) and not parsed.path.startswith('/jobs/view/'): continue
        return value
    return fallback


def description_text(markup):
    """Preserve paragraph/list boundaries without splitting inline emphasis."""
    if not markup:
        return ''
    for tag in markup.select('script, style, iframe, noscript'):
        tag.decompose()
    for br in markup.find_all('br'):
        br.replace_with('\n')
    for tag in list(markup.find_all(['p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])):
        if tag.find(['p', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            continue
        text = re.sub(r'[ \t]+', ' ', tag.get_text(' ', strip=True)).strip()
        tag.replace_with(('\n- ' if tag.name == 'li' else '\n\n') + text + '\n\n')
    text = markup.get_text(' ', strip=False)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return re.sub(r'(?:\s*Show more\s*Show less)\s*$', '', text, flags=re.I).strip()


def parse_detail(html, card, company):
    soup = BeautifulSoup(html, "html.parser")
    markup = soup.select_one(".show-more-less-html__markup, .description__text")
    closed_element=soup.select_one('.closed-job__flavor')
    closed=bool(closed_element and re.search(r'no longer accepting applications|no longer available',closed_element.get_text(' ',strip=True),re.I))
    if not markup and not closed:
        raise CollectionError("Job description was not accessible")
    title = soup.select_one(".top-card-layout__title, h1")
    employer = soup.select_one(".topcard__org-name-link")
    location = soup.select_one(".topcard__flavor--bullet")
    source = f"https://www.linkedin.com/jobs/view/{card['id']}/"
    original_description = markup.get_text("\n", strip=True) if markup else ''
    full_description = description_text(markup)
    description = full_description[:12000]
    if closed:
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
    return {**card, **criteria, "availability": "closed" if description.startswith('No longer accepting applications.') else "open", "descriptionTruncated":len(full_description)>12000, "applicationType":"external" if 'linkedin.com' not in (urlparse(application_url(soup,source)).hostname or '') else 'linkedin', "companyLogo": logo_url, "companyUrl": f'https://www.linkedin.com/company/{slug}/' if slug else '', "detailComplete": True, "companyId": slug, "title": title.get_text(" ", strip=True) if title else card["title"], "company": employer.get_text(" ", strip=True) if employer else card["company"], "location": location.get_text(" ", strip=True) if location else card["location"], "sourceUrl": source, "applicationUrl": application_url(soup, source), "description": description, "originalDescription": original_description[:20000]}


def _compat_collect(watchlist,mode,max_companies=5,browser='never',pages=3,max_jobs=100,pause=4,fetcher=None,cache=None,known_ids=None,keywords=None,location='',shard=0,shards=1):
    """Local companion compatibility; production uses the authenticated checkpoint protocol."""
    from engine import run_collection
    import hashlib
    watchlist={**watchlist,'companies':[c for c in watchlist['companies'] if int(hashlib.sha256(company_url(c['url']).split('/')[4].encode()).hexdigest(),16)%shards==shard]}
    saved=cache if cache is not None else {'companies':{},'jobs':{}}
    progress=saved.setdefault('progress',{})
    for slug,value in saved.get('companies',{}).items():
        progress.setdefault('company:'+slug,{'page':0,**({'attemptedAt':value['lastChecked']} if value.get('lastChecked') else {}),**({'numericId':value['numericId']} if value.get('numericId') else {})})
    known=[{'id':ident,'status':'approved','firstSeen':now(),'postedAt':now(),'descriptionLength':120,'applicationUrl':f'https://www.linkedin.com/jobs/view/{ident}/'} for ident in known_ids or []]
    snapshot={'watchlist':watchlist,'jobs':{'items':known},'queue':{'items':[]},'collector':{'progress':progress}}
    def checkpoint(body):
        progress.update(body['progress'])
        for key,value in body['progress'].items():
            if key.startswith('company:'):saved.setdefault('companies',{}).setdefault(key[8:],{}).update({'lastChecked':value.get('attemptedAt',''),**({'numericId':value['numericId']} if value.get('numericId') else {})})
    return run_collection(snapshot,mode=mode,keywords=keywords,location=location,pages=pages,max_companies=max_companies,max_jobs=max_jobs,fetcher=fetcher or Fetcher(browser,pause),callback=checkpoint)


def collect_search(watchlist,keywords=None,location='',browser='never',pages=3,max_jobs=100,pause=4,fetcher=None,cache=None,known_ids=None):
    return _compat_collect(watchlist,'search',browser=browser,pages=pages,max_jobs=max_jobs,pause=pause,fetcher=fetcher,cache=cache,known_ids=known_ids,keywords=keywords,location=location)


def collect(watchlist,max_companies=5,browser='never',pages=3,max_jobs=300,pause=4,fetcher=None,cache=None,known_ids=None,shard=0,shards=1):
    return _compat_collect(watchlist,'companies',max_companies=max_companies,browser=browser,pages=pages,max_jobs=max_jobs,pause=pause,fetcher=fetcher,cache=cache,known_ids=known_ids,shard=shard,shards=shards)


def load_cache(path):
    value=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'companies':{},'jobs':{}}
    if not isinstance(value.get('companies'),dict) or not isinstance(value.get('jobs'),dict):
        raise CollectionError('Collector cache is invalid; preserve it for recovery before starting again.')
    seeds=ROOT/'data/company-ids.json'
    if seeds.exists():
        for slug,numeric in json.loads(seeds.read_text(encoding='utf-8')).items():
            if COMPANY_PATH.fullmatch('/company/'+slug+'/') and re.fullmatch(r'\d{1,20}',str(numeric)):
                value['companies'].setdefault(slug,{}).setdefault('numericId',str(numeric))
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
            response = requests.post(url, data=payload, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Origin": origin}, timeout=60, allow_redirects=False)
            if response.ok:
                return
            if response.status_code not in (409, 429, 502, 503):
                raise CollectionError(f"Results were not accepted (HTTP {response.status_code})")
        except requests.RequestException:
            pass
        time.sleep(2 ** attempt)
    raise CollectionError("Results could not be uploaded; result.json is retained for manual import")


def main():
    from engine import run_collection
    from protocol import CollectorClient, write_summary
    from logos import LogoCache
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['search','companies','verify','maintenance'],default='search')
    parser.add_argument('--watchlist',type=Path,default=ROOT/'data/watchlist.json')
    parser.add_argument('--watchlist-url',default=os.getenv('JOBS_WATCHLIST_URL'))
    parser.add_argument('--state-url',default='')
    parser.add_argument('--keywords-json',default=os.getenv('JOBS_KEYWORDS_JSON',''))
    parser.add_argument('--location',default=os.getenv('JOBS_LOCATION',''))
    parser.add_argument('--company',default=os.getenv('JOBS_COMPANY',''))
    parser.add_argument('--max-companies',type=int,default=5)
    parser.add_argument('--max-jobs',type=int,default=100)
    parser.add_argument('--pages',type=int,default=3)
    parser.add_argument('--request-budget',type=int,default=80)
    parser.add_argument('--browser',choices=['never','auto'],default='never')
    parser.add_argument('--pause',type=float,default=4)
    parser.add_argument('--submit',action='store_true')
    args=parser.parse_args()
    if not 1<=args.max_companies<=2000 or not 1<=args.max_jobs<=300 or not 1<=args.pages<=10 or not 1<=args.request_budget<=150 or args.pause<4:
        parser.error('Use 1–2000 companies, 1–300 jobs, 1–10 pages, 1–150 source requests, and pause >=4 seconds')
    keywords=json.loads(args.keywords_json) if args.keywords_json else DEFAULT_KEYWORDS
    if not isinstance(keywords,list) or not 1<=len(keywords)<=8 or any(not isinstance(k,str) or not k.strip() or len(k)>100 for k in keywords):parser.error('Use a JSON array of 1–8 keywords')
    token=os.getenv('JOBS_INGEST_TOKEN','')
    client=None
    if token:
        client=CollectorClient(os.getenv('JOBS_INGEST_URL',''),token)
        snapshot=client.start()
    else:
        if args.submit or os.getenv('GITHUB_ACTIONS'):raise CollectionError('The authenticated collector connection is required before source requests')
        if args.watchlist_url:
            response=requests.get(args.watchlist_url,timeout=30);response.raise_for_status();watchlist=response.json()
        else:watchlist=json.loads(args.watchlist.read_text(encoding='utf-8-sig'))
        snapshot={'watchlist':watchlist,'jobs':{'items':[]},'queue':{'items':[]},'collector':{'progress':{}}}
    if args.company:
        if args.mode!='companies':raise CollectionError('A targeted company requires companies mode')
        snapshot['watchlist']={**snapshot['watchlist'],'companies':[c for c in snapshot['watchlist']['companies'] if company_url(c['url']).split('/')[4]==args.company]}
        if not companies_from(snapshot['watchlist']):raise CollectionError('Target company is not active in the watchlist')
    logos=LogoCache(snapshot.get('logos',{}),snapshot.get('jobs',{}).get('items',[])+snapshot.get('queue',{}).get('items',[]))
    latest={}
    final_sent=False
    def checkpoint(body):
        nonlocal latest,final_sent
        if args.submit:logos.enrich(body)
        latest=body
        if not args.submit:save_result(body)
        if args.submit:
            client.checkpoint(body)
            if body.get('final'):final_sent=True
        elif client:client.heartbeat()
    try:
        result=run_collection(snapshot,mode=args.mode,keywords=keywords,
            location=args.location,pages=args.pages,max_companies=args.max_companies,max_jobs=args.max_jobs,
            request_budget=args.request_budget,fetcher=Fetcher(args.browser,args.pause),callback=checkpoint,session_id=client.id if client else None)
    finally:
        if client and not final_sent:client.finish_dry(latest.get('run',{}).get('outcome','failed') if not args.submit else 'blocked' if latest.get('run',{}).get('outcome')=='blocked' or any(p.get('status')=='blocked' for p in latest.get('progress',{}).values()) else 'failed')
    # Full local recovery output is separate from the last idempotent delivery chunk.
    (ROOT/'output').mkdir(exist_ok=True)
    (ROOT/'output/full-result.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
    write_summary(result,args.submit)
    if result['run']['outcome'] in ('blocked','failed'):raise CollectionError('Collection '+result['run']['outcome']+'; inspect the saved coverage report before retrying')


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, requests.RequestException, ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
