"""Check shared control before installing dependencies. Uses only the Python standard library."""
from datetime import datetime, timezone
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def check():
    url=os.getenv('JOBS_INGEST_URL','');token=os.getenv('JOBS_INGEST_TOKEN','')
    parsed=urlparse(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.port or len(token)<32:
        raise RuntimeError('The authenticated collector connection is missing or invalid')
    request=urllib.request.Request(url,headers={'Authorization':'Bearer '+token,'User-Agent':'AccesoJobsCollector/1.0','Accept':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=30) as response:
            control=json.load(response).get('control',{})
    except urllib.error.HTTPError as error:raise RuntimeError(f'Collector preflight failed (HTTP {error.code}); no source requests were made') from None
    except (urllib.error.URLError,ValueError):raise RuntimeError('Collector preflight connection failed; no source requests were made') from None
    until=control.get('cooldownUntil',0)/1000
    if until>time.time():
        raise RuntimeError('Collector cooling down until '+datetime.fromtimestamp(until,timezone.utc).isoformat()+'. No dependencies installed or source requests made.')
    if (control.get('active') or {}).get('until',0)/1000>time.time():raise RuntimeError('Another collector session is active; no source requests were made')
    print('Collector control is available. The runtime will acquire its lease before source requests.')

if __name__=='__main__':
    try:check()
    except RuntimeError as error:print(str(error),file=sys.stderr);raise SystemExit(1)
