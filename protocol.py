"""Authenticated state and idempotent page delivery. No R2 credentials required."""
import json
import os
import uuid
from urllib.parse import urlparse
import requests
from scraper import ROOT, CollectionError, save_result, submit_result


class DeliveryError(RuntimeError):
    pass


class CollectorClient:
    def __init__(self, url, token):
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port or not parsed.hostname:
            raise CollectionError('Collector endpoint must be a plain HTTPS URL')
        if len(token)<32:raise CollectionError('Collector delivery secret is missing')
        self.url,self.token=url,token
        self.id=os.getenv('JOBS_REQUEST_ID') or uuid.uuid4().hex

    def start(self):
        parsed=urlparse(self.url)
        try:
            response=requests.post(self.url,json={'action':'start','id':self.id},headers={'Authorization':'Bearer '+self.token,'Origin':f'{parsed.scheme}://{parsed.netloc}'},timeout=45,allow_redirects=False)
            if response.status_code in (409,429):raise CollectionError(response.json().get('error','Collector is busy or cooling down'))
            if not response.ok:raise CollectionError(f'Collector session could not start (HTTP {response.status_code})')
            return response.json()
        except requests.RequestException:raise CollectionError('Collector session connection failed; no source requests were made') from None

    def checkpoint(self,body):
        save_result(body)
        try:submit_result(body,self.url,self.token)
        except Exception as error:raise DeliveryError(str(error)) from None

    def finish_dry(self,outcome):
        # Dry runs obey the same shared lease/cooldown but do not change jobs or coverage.
        body={'action':'finish','id':self.id,'outcome':outcome}
        try:submit_result(body,self.url,self.token)
        except Exception as error:raise DeliveryError(str(error)) from None

    def heartbeat(self):
        try:submit_result({'action':'heartbeat','id':self.id},self.url,self.token)
        except Exception as error:raise DeliveryError(str(error)) from None


def write_summary(result,submit):
    run=result['run']
    print(f"{run['mode']}: {run['outcome']}; {run['checked']} pages; {run['requests']} source fetches; {run['found']} candidates; {run['refreshed']} refreshed; {run['skippedKnown']} known skipped")
    destination=os.getenv('GITHUB_STEP_SUMMARY')
    if destination:
        # Source text is escaped; never render raw source HTML in a workflow summary.
        import html
        with open(destination,'a',encoding='utf-8') as stream:
            stream.write(f"## Collection: {run['outcome']}\n\nMode: {run['mode']}. Delivery: {'enabled' if submit else 'dry run'}.\n\n{run['checked']} pages · {run['requests']} source fetches · {run['found']} candidates · {run['refreshed']} refreshed · {run['skippedKnown']} known skipped.\n\n")
            for message in run['warnings']:stream.write('- Coverage: '+html.escape(message)+'\n')
            for error in run['errors']:stream.write('- Error: '+html.escape(error['message'])+'\n')
