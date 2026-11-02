"""Explicit manual access diagnostic; no credentials, storage writes, or watchlist."""
import json
from engine import run_collection
from scraper import Fetcher

if __name__ == '__main__':
    result=run_collection(
        {'watchlist':{'version':1,'companies':[]},'jobs':{'items':[]},'queue':{'items':[]}},
        mode='search',keywords=['industrial designer'],location='Worldwide',
        pages=1,max_jobs=3,request_budget=4,fetcher=Fetcher('never',4))
    report={'run':result['run'],'jobs':[{'id':j['id'],'title':j['title'],'company':j['company']} for j in result['items']]}
    print(json.dumps(report,indent=2))
    raise SystemExit(1 if result['run']['outcome'] in ('blocked','failed') else 0)
