import copy
from datetime import datetime, timedelta, timezone
import unittest
from urllib.parse import parse_qs, urlparse
from engine import run_collection
from scraper import RateLimited, AccessBlocked, CollectionError, parse_detail
from test_scraper import COMPANY, CARD, DETAIL

def date(days=0):return (datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
def snapshot():return {'watchlist':{'version':1,'companies':[COMPANY]},'jobs':{'items':[]},'queue':{'items':[]},'collector':{'progress':{}}}
class Fake:
    def __init__(self,search=CARD,detail=DETAIL):self.search=search;self.detail=detail;self.urls=[];self.closed=False
    def get(self,url):
        self.urls.append(url)
        response=self.detail if '/jobPosting/' in url else self.search
        if isinstance(response,Exception):raise response
        return response(url) if callable(response) else response
    def close(self):self.closed=True

class EngineTests(unittest.TestCase):
    def test_cursor_survives_partial_page_and_remaining_jobs_are_discovered(self):
        state=snapshot();pages=[]
        html=''.join(CARD.replace('12345678',str(12345678+i)) for i in range(6))
        first=run_collection(state,keywords=['industrial designer'],pages=1,max_jobs=3,fetcher=Fake(search=html),callback=lambda body:pages.append(copy.deepcopy(body)))
        self.assertEqual(first['run']['outcome'],'partial');self.assertEqual(len(first['items']),3)
        for body in pages:state['collector']['progress'].update(body['progress'])
        self.assertEqual(next(iter(state['collector']['progress'].values()))['page'],0)
        state['jobs']['items']=[{**j,'firstSeen':date(),'descriptionLength':200,'lastVerifiedAt':date(),'status':'approved'} for j in first['items']]
        second=run_collection(state,keywords=['industrial designer'],pages=1,max_jobs=3,fetcher=Fake(search=html))
        self.assertEqual([j['id'] for j in second['items']],['12345681','12345682','12345683'])

    def test_every_page_checkpoint_advances_and_search_rotates_fairly(self):
        state=snapshot();saved=[]
        def html(url):return CARD.replace('12345678',str(12345678+int(parse_qs(urlparse(url).query).get('start',['0'])[0])))
        run_collection(state,keywords=['industrial designer'],pages=2,fetcher=Fake(search=html),callback=lambda body:saved.append(copy.deepcopy(body)))
        for body in saved:state['collector']['progress'].update(body['progress'])
        self.assertEqual(next(iter(state['collector']['progress'].values()))['page'],2)
        fetcher=Fake()
        run_collection(state,keywords=['industrial designer','CMF designer'],pages=1,max_jobs=1,fetcher=fetcher)
        self.assertIn('CMF+designer',fetcher.urls[0])

    def test_rate_limit_and_login_wall_stop_all_remaining_tasks(self):
        for error in (RateLimited('429'),AccessBlocked('login wall')):
            fetcher=Fake(search=error);saved=[]
            result=run_collection(snapshot(),keywords=['industrial designer','CMF designer'],fetcher=fetcher,callback=lambda body:saved.append(copy.deepcopy(body)))
            self.assertEqual(len(fetcher.urls),1);self.assertEqual(result['run']['outcome'],'blocked');self.assertTrue(saved[-1]['final']);self.assertTrue(fetcher.closed)

    def test_known_approved_jobs_are_reverified_when_due(self):
        state=snapshot();state['jobs']['items']=[{'id':'12345678','companyId':'fixture','company':'Fixture','title':'Industrial Designer','location':'Milan','postedAt':date(-20),'firstSeen':date(-20),'lastVerifiedAt':date(-8),'descriptionLength':200,'applicationUrl':'https://careers.example/job','status':'approved'}]
        fetcher=Fake(detail='<span class="closed-job__flavor">No longer accepting applications</span>')
        result=run_collection(state,mode='verify',fetcher=fetcher)
        self.assertEqual(result['run']['refreshed'],1);self.assertEqual(result['items'][0]['availability'],'closed')
        self.assertEqual(len(fetcher.urls),1)

    def test_failed_verification_does_not_invent_closure(self):
        state=snapshot();state['jobs']['items']=[{'id':'12345678','title':'Industrial Designer','companyId':'fixture','company':'Fixture','location':'Milan','firstSeen':date(-20),'postedAt':date(-20),'lastVerifiedAt':date(-8),'status':'approved'}]
        saved=[];result=run_collection(state,mode='verify',fetcher=Fake(detail=CollectionError('404')),callback=lambda body:saved.append(copy.deepcopy(body)))
        self.assertEqual(result['items'],[]);self.assertIn('12345678',saved[0]['verificationFailures']);self.assertEqual(result['run']['outcome'],'failed')

    def test_request_budget_is_enforced_and_discarded_ids_are_suppressed(self):
        state=snapshot();state['tombstones']=[{'id':'12345678','until':date(20)}]
        result=run_collection(state,keywords=['industrial designer'],pages=1,fetcher=Fake(),request_budget=1)
        self.assertEqual(result['items'],[]);self.assertEqual(result['run']['requests'],1)
        result=run_collection(snapshot(),keywords=['industrial designer'],pages=1,fetcher=Fake(),request_budget=1)
        self.assertEqual(result['run']['outcome'],'partial');self.assertEqual(result['run']['requests'],1)

    def test_checkpoint_failure_stops_source_work_and_closes_fetcher(self):
        fetcher=Fake()
        def reject(body):raise RuntimeError('delivery unavailable')
        with self.assertRaises(RuntimeError):run_collection(snapshot(),keywords=['industrial designer'],fetcher=fetcher,callback=reject)
        self.assertTrue(fetcher.closed);self.assertEqual(len(fetcher.urls),2)

    def test_maintenance_uses_no_source_requests(self):
        fetcher=Fake();run_collection(snapshot(),mode='maintenance',fetcher=fetcher)
        self.assertEqual(fetcher.urls,[])

    def test_obvious_digital_roles_skip_details_but_physical_roles_with_ux_mentions_survive(self):
        fetcher=Fake(search=CARD.replace('Industrial Designer','UX Designer'))
        result=run_collection(snapshot(),keywords=['industrial designer'],pages=1,fetcher=fetcher)
        self.assertEqual(result['items'],[]);self.assertEqual(len(fetcher.urls),1)
        fetcher=Fake(search=CARD.replace('Industrial Designer','Industrial Designer with UX experience'))
        self.assertEqual(len(run_collection(snapshot(),keywords=['industrial designer'],pages=1,fetcher=fetcher)['items']),1)

    def test_source_ui_labels_are_cleaned_and_truncation_is_explicit(self):
        card={'id':'12345678','title':'Industrial Designer','company':'Fixture','location':'Milan'}
        parsed=parse_detail(DETAIL.replace('manufacturing.','manufacturing.\nShow more\nShow less'),card,COMPANY)
        self.assertNotIn('Show less',parsed['description'])
        parsed=parse_detail(DETAIL.replace('manufacturing.','x'*13000),card,COMPANY)
        self.assertTrue(parsed['descriptionTruncated']);self.assertEqual(len(parsed['description']),12000)

if __name__=='__main__':unittest.main()
