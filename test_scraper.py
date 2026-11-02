import unittest
from unittest.mock import Mock
import requests
from scraper import Fetcher, collect_search, RateLimited
from scraper import CollectionError, application_url, collect, companies_from, job_cards, organization_id, parse_detail, wall
from bs4 import BeautifulSoup

COMPANY = {"id": "fixture", "name": "Fixture", "url": "https://www.linkedin.com/company/fixture/", "linkedinId": "123"}
CARD = '<li><div class="base-search-card"><a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/industrial-designer-at-fixture-12345678?tracking=x"></a><h3 class="base-search-card__title">Industrial Designer</h3><h4 class="base-search-card__subtitle">Fixture</h4><span class="job-search-card__location">Milan</span><time datetime="2026-10-25"></time></div></li>'
DETAIL = '<h1>Industrial Designer</h1><a class="topcard__org-name-link">Fixture</a><span class="topcard__flavor--bullet">Milan</span><div class="show-more-less-html__markup">Sketch physical products, build prototypes and collaborate with manufacturing.</div><a class="apply-button" href="https://www.linkedin.com/jobs/view/externalApply/123?url=https%3A%2F%2Fcareers.example%2F123">Apply</a>'


class FakeFetcher:
    def __init__(self, detail=DETAIL): self.detail = detail
    def get(self, url):
        if "jobPosting" in url:
            if self.detail is None: raise CollectionError("Blocked description")
            return self.detail
        return CARD
    def close(self): pass


class ScraperTests(unittest.TestCase):
    def test_company_jobs_tab_resolves_id_when_profile_omits_it(self):
        company={**COMPANY,'linkedinId':''}
        fetcher=FakeFetcher()
        def get(url):
            if url==company['url']:return '<h1>Public profile</h1>'
            if url==company['url']+'jobs/':return '"companyId":123'
            return DETAIL if 'jobPosting' in url else CARD
        fetcher.get=Mock(side_effect=get)
        result=collect({'version':1,'companies':[company]},pages=1,fetcher=fetcher)
        self.assertEqual(len(result['items']),1)
        self.assertTrue(any(call.args[0].endswith('/jobs/') for call in fetcher.get.call_args_list))
    def test_primary_search_discovers_companies_outside_watchlist(self):
        fetcher=FakeFetcher()
        fetcher.get=Mock(wraps=fetcher.get)
        result=collect_search({'version':1,'companies':[]},keywords=['industrial designer'],pages=1,fetcher=fetcher)
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['collectionSource'],'search')
        self.assertNotIn('f_C=',fetcher.get.call_args_list[0].args[0])
        self.assertEqual(result['run']['mode'],'search')
        self.assertTrue(result['run']['finishedAt'])

    def test_primary_search_deduplicates_keywords_and_skips_known(self):
        result=collect_search({'version':1,'companies':[]},keywords=['industrial designer','CMF'],fetcher=FakeFetcher(),known_ids=['12345678'])
        self.assertEqual(result['items'],[])
        self.assertEqual(result['run']['skippedKnown'],1)

    def test_primary_search_stops_at_rate_limit(self):
        fetcher=FakeFetcher();fetcher.get=Mock(side_effect=RateLimited('429: stop'))
        result=collect_search({'version':1,'companies':[]},fetcher=fetcher)
        self.assertEqual(fetcher.get.call_count,1)
        self.assertEqual(len(result['run']['errors']),1)
        self.assertTrue(result['run']['finishedAt'])

    def test_company_logo_identity_and_criteria(self):
        html=CARD.replace('Fixture</h4>','<a href="https://www.linkedin.com/company/fixture/?tracking=1">Fixture</a></h4>').replace('<time','<img data-delayed-url="https://media.licdn.com/logo.png"><time')
        card=job_cards(html)[0]
        self.assertEqual(card['companyId'],'fixture')
        self.assertEqual(card['companyLogo'],'https://media.licdn.com/logo.png')
        detail=parse_detail(DETAIL+'<li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Employment type</h3><span class="description__job-criteria-text">Full-time</span></li>',card,COMPANY)
        self.assertEqual(detail['employmentType'],'Full-time')

    def test_saved_details_are_reused_and_known_ids_skip_detail_requests(self):
        cache={'companies':{},'jobs':{}}
        first=collect({'version':1,'companies':[COMPANY]},fetcher=FakeFetcher(),cache=cache)
        self.assertEqual(len(first['items']),1)
        fetcher=FakeFetcher(detail=None)
        second=collect({'version':1,'companies':[COMPANY]},fetcher=fetcher,cache=cache)
        self.assertEqual(second['run']['cachedDetails'],1)
        self.assertEqual(second['items'],first['items'])
        third=collect({'version':1,'companies':[COMPANY]},fetcher=fetcher,cache=cache,known_ids={'12345678'})
        self.assertEqual(third['items'],[])
        self.assertEqual(third['run']['skippedKnown'],1)

    def test_incomplete_details_retry_and_company_rotation_avoids_starvation(self):
        cache={'companies':{},'jobs':{}}
        watch={'version':1,'companies':[COMPANY,{**COMPANY,'url':'https://www.linkedin.com/company/second/','name':'Second'}]}
        collect(watch,max_companies=1,fetcher=FakeFetcher(detail=None),cache=cache)
        self.assertEqual(cache['jobs'],{})
        second=collect(watch,max_companies=1,fetcher=FakeFetcher(),cache=cache)
        self.assertEqual(second['items'][0]['companyId'],'second')

    def test_shards_cover_each_company_exactly_once(self):
        watch={'version':1,'companies':[{**COMPANY,'url':f'https://www.linkedin.com/company/fixture-{i}/'} for i in range(40)]}
        owners=[]
        for shard in range(4):
            cache={'companies':{},'jobs':{}}
            collect(watch,max_companies=100,fetcher=FakeFetcher(),cache=cache,shard=shard,shards=4)
            owners.extend(cache['companies'])
        self.assertEqual(len(owners),40)
        self.assertEqual(len(set(owners)),40)

    def test_connection_failure_reaches_browser_fallback(self):
        fetcher = Fetcher('auto', pause=0)
        fetcher.session.get = Mock(side_effect=requests.ConnectionError('Network unavailable'))
        fetcher._browser_get = Mock(return_value='browser content')
        self.assertEqual(fetcher.get('https://www.linkedin.com/company/fixture/'), 'browser content')
        fetcher._browser_get.assert_called_once()
        fetcher.close()

    def test_company_validation_and_pause(self):
        self.assertEqual(len(companies_from({'version':1,'companies':[{**COMPANY,'url':'https://www.linkedin.com/company/studio-%c3%a9/'}]})),1)
        self.assertEqual(len(companies_from({"version": 1, "companies": [COMPANY]})), 1)
        self.assertEqual(companies_from({"version": 1, "companies": [{**COMPANY, "enabled": False}]}), [])
        with self.assertRaises(CollectionError): companies_from({"version": 1, "companies": [{**COMPANY, "url": "https://evil.test/"}]})

    def test_ids_and_cards(self):
        self.assertEqual(organization_id('"companyId":123'), "123")
        cards = job_cards(CARD + CARD)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], "12345678")
        self.assertEqual(cards[0]["company"], "Fixture")

    def test_detail_and_application(self):
        detail = parse_detail(DETAIL, job_cards(CARD)[0], COMPANY)
        self.assertEqual(detail["applicationUrl"], "https://careers.example/123")
        self.assertEqual(detail["companyId"], "fixture")
        self.assertIn("physical products", detail["description"])
        self.assertEqual(application_url(BeautifulSoup('<a class="apply-button" href="javascript:evil()">Apply</a>', 'html.parser'), 'safe'), 'safe')
        self.assertEqual(application_url(BeautifulSoup('<a class="apply-button" href="https://www.linkedin.com/signup/cold-join?x=1">Apply</a>','html.parser'),'canonical-job'),'canonical-job')
        self.assertEqual(application_url(BeautifulSoup('<a class="apply-button" href="https://www.linkedin.com/signup/cold-join">Apply</a><code id="applyUrl">"https://careers.example/direct"</code>','html.parser'),'canonical-job'),'https://careers.example/direct')

    def test_blocked_pages_and_missing_description(self):
        self.assertTrue(wall('<title>Just a moment...</title>'))
        self.assertFalse(wall('<title>Fixture | LinkedIn</title><form action="/login-submit"></form><main>Public company content</main>'))
        with self.assertRaises(CollectionError): parse_detail('<html>Login</html>', job_cards(CARD)[0], COMPANY)

    def test_failed_detail_is_queued_as_incomplete_not_silently_lost(self):
        result = collect({"version": 1, "companies": [COMPANY]}, fetcher=FakeFetcher(None))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["description"], '')
        self.assertEqual(len(result["run"]["errors"]), 1)

    def test_collection_produces_idempotency_key_and_raw_candidates(self):
        result = collect({"version": 1, "companies": [COMPANY]}, fetcher=FakeFetcher())
        self.assertEqual(result["run"]["checked"], 1)
        self.assertEqual(result["run"]["found"], 1)
        self.assertEqual(result["run"]["errors"], [])
        self.assertNotIn('status', result['items'][0])


if __name__ == '__main__': unittest.main()
