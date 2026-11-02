"""Bounded collection with durable page checkpoints and independent verification."""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid
from urllib.parse import urlencode
from scraper import (Fetcher, CollectionError, RateLimited, AccessBlocked, DESIGN_SIGNAL,
                     DEFAULT_KEYWORDS, SEARCH_TERMS, ROOT, companies_from, job_cards,
                     organization_id, parse_detail, now)

DAY = 86400


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


class BudgetReached(CollectionError):
    pass


def run_collection(snapshot, mode='search', keywords=None, location='', pages=3,
                   max_companies=5, max_jobs=100, request_budget=80, fetcher=None,
                   callback=None, session_id=None, clock=time.time):
    keywords = keywords or DEFAULT_KEYWORDS
    if not 1 <= len(keywords) <= 8 or any(not isinstance(k,str) or not k.strip() or len(k)>100 for k in keywords):
        raise CollectionError('Supply 1–8 search keywords of at most 100 characters')
    companies = companies_from(snapshot['watchlist'])
    by_company = {c['id']: c for c in companies}
    paused = {c['id'] for c in snapshot['watchlist']['companies'] if c.get('enabled') is False}
    known = {str(j['id']):j for j in snapshot.get('jobs',{}).get('items',[])+snapshot.get('queue',{}).get('items',[])}
    suppressed = {str(t['id']) for t in snapshot.get('tombstones',[]) if timestamp(t.get('until'))>clock()}
    progress = dict(snapshot.get('collector',{}).get('progress',{}))
    seed_path=ROOT/'data/company-ids.json'
    seeds=json.loads(seed_path.read_text(encoding='utf-8')) if seed_path.exists() else {}
    source=fetcher or Fetcher('never',4)
    run={'id':session_id or uuid.uuid4().hex,'mode':mode,'startedAt':now(),'finishedAt':'','checked':0,'found':0,
         'outcome':'complete','warnings':[],'errors':[],'requests':0,'skippedKnown':0,'refreshed':0}
    all_items, pending, observations, failures, changes = [], [], set(), set(), {}
    seen=set()
    deadline=clock()+35*60
    sequence=0

    def get(url):
        if run['requests']>=request_budget or clock()>=deadline:
            raise BudgetReached('Request or time budget reached; remaining coverage is deferred')
        run['requests']+=1
        return source.get(url)

    def emit(final=False):
        nonlocal sequence,pending,observations,failures,changes
        run['finishedAt']=now()
        run['found']=len(all_items)
        body={'action':'checkpoint','sessionId':run['id'],'deliveryId':f"{run['id']}-{sequence}",'final':final,
              'items':list(pending),'observedIds':sorted(observations),'verificationFailures':sorted(failures),
              'progress':dict(changes),'run':dict(run)}
        if callback: callback(body)
        sequence+=1
        pending,observations,failures,changes=[],set(),set(),{}

    def record(card, company=None, refresh=False):
        ident=card['id']
        observations.add(ident)
        if ident in suppressed or ident in seen or card.get('companyId') in paused:
            return
        seen.add(ident)
        old=known.get(ident)
        if old and not refresh and (old.get('status')=='discarded' or old.get('availability')=='closed' or old.get('descriptionLength',len(old.get('description','')))>=120 and old.get('applicationUrl') and old.get('postedAt')):
            run['skippedKnown']+=1
            return
        if not DESIGN_SIGNAL.search(card.get('title','')) and not refresh:
            return
        if not refresh and re.search(r'\b(?:UX|UI|software|frontend|backend|web|graphic|electrical|civil|PCB)\b',card.get('title',''),re.I) and not re.search(r'industrial|\bCMF\b|footwear|structural packaging|physical product|automotive|clay',card.get('title',''),re.I):
            return
        if len(all_items)>=max_jobs:
            raise BudgetReached('Candidate budget reached; resume the current page next time')
        card={key:card.get(key,'') for key in ('id','title','company','companyId','companyUrl','location','postedAt','companyLogo')}
        try:
            item=parse_detail(get(f'https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{ident}'),card,company or {'id':card.get('companyId','')})
            item['collectionSource']=mode if mode in ('search','companies') else old.get('collectionSource','') if old else ''
            if refresh:run['refreshed']+=1
        except (RateLimited,AccessBlocked,BudgetReached):
            failures.add(ident)
            raise
        except CollectionError as error:
            failures.add(ident)
            if old:
                run['errors'].append({'companyId':card.get('companyId',''),'message':f'Verification unavailable for {ident}: {error}'})
                return
            item={**card,'detailComplete':False,'description':'','applicationUrl':'','collectionSource':mode}
            run['errors'].append({'companyId':card.get('companyId',''),'message':f'Incomplete job {ident}: {error}'})
        pending.append(item);all_items.append(item)

    # A small, separately bounded verification pass prevents approved jobs from becoming immortal.
    due=[]
    for job in known.values():
        age=timestamp(job.get('postedAt') or job.get('firstSeen'))
        verified=timestamp(job.get('lastVerifiedAt') or job.get('firstSeen'))
        attempted=timestamp(job.get('verificationAttemptedAt'))
        if job.get('status')!='discarded' and job.get('availability')!='closed' and age+90*DAY>clock() and verified+7*DAY<=clock() and attempted+DAY<=clock():
            due.append(job)
    due.sort(key=lambda j:timestamp(j.get('lastVerifiedAt') or j.get('firstSeen')))
    try:
        verify_limit=0 if mode=='maintenance' else min(5,max(1,max_jobs//4)) if mode!='verify' else max_jobs
        for job in due[:verify_limit]:
            record(job,by_company.get(job.get('companyId')),refresh=True)
            emit()
        if mode=='maintenance':
            pass
        elif mode=='verify':
            if len(due)>verify_limit:run['warnings'].append('Verification budget reached; more stale jobs remain')
        else:
            if mode=='companies':
                tasks=[('company:'+c['id'],c) for c in companies]
            else:
                tasks=[]
                for keyword in keywords:
                    digest=hashlib.sha256(json.dumps([keyword.strip().lower(),location.strip().lower()],ensure_ascii=False).encode()).hexdigest()[:32]
                    tasks.append(('search:'+digest,keyword.strip()))
            tasks=[task for task in tasks if timestamp(progress.get(task[0],{}).get('retryAfter'))<=clock()]
            tasks.sort(key=lambda task:progress.get(task[0],{}).get('attemptedAt',''))
            if mode=='companies':tasks=tasks[:max_companies]
            for key,value in tasks:
                previous=dict(progress.get(key,{}))
                entry={**previous,'page':previous.get('page',0),'attemptedAt':now(),'status':'partial'}
                changes[key]=entry
                signatures=set()
                try:
                    numeric=''
                    if mode=='companies':
                        numeric=value.get('linkedinId') or previous.get('numericId') or seeds.get(value['id'])
                        if not numeric:
                            try:numeric=organization_id(get(value['url']))
                            except (RateLimited,AccessBlocked,BudgetReached):raise
                            except CollectionError:numeric=organization_id(get(value['url']+'jobs/'))
                        entry['numericId']=numeric
                    for _ in range(pages):
                        changes[key]=entry
                        page=entry['page']
                        query={'keywords':SEARCH_TERMS if mode=='companies' else value,'location':location.strip() or 'Worldwide','start':page*10,'sortBy':'DD','f_TPR':'r7776000'}
                        if numeric:query['f_C']=numeric
                        html=get('https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?'+urlencode(query))
                        cards=job_cards(html)
                        run['checked']+=1
                        signature=tuple(c['id'] for c in cards)
                        if not cards:
                            if html.strip() and not any(s in html.lower() for s in ('no results','no jobs','no matching','couldn.t find')):
                                raise CollectionError('Unrecognized HTML; coverage is inconclusive')
                            entry.update(page=0,status='complete',successAt=now(),retryAfter='')
                            emit();break
                        if signature in signatures:
                            raise CollectionError('Repeated result page; source pagination is inconclusive')
                        signatures.add(signature)
                        for card in cards:
                            if mode=='companies':card={**card,'companyId':value['id'],'companyUrl':value['url']}
                            record(card,value if mode=='companies' else None)
                        entry['page']=page+1
                        if entry['page']>=100:
                            entry['page']=0
                            run['warnings'].append('Deep pagination ceiling reached; search cycle reset')
                        emit()
                    else:
                        run['warnings'].append(('Company '+value['id'] if mode=='companies' else value)+': page budget reached; cursor saved')
                except (RateLimited,AccessBlocked):
                    entry['status']='blocked';changes[key]=entry;emit();raise
                except BudgetReached:
                    entry['status']='partial';changes[key]=entry;emit();raise
                except CollectionError as error:
                    entry.update(status='failed',retryAfter=datetime.fromtimestamp(clock()+DAY,timezone.utc).isoformat())
                    run['errors'].append({'companyId':value['id'] if mode=='companies' else '', 'message':str(error)[:300]})
                    changes[key]=entry
                    emit()
                progress[key]=dict(entry)
    except (RateLimited,AccessBlocked) as error:
        run['outcome']='blocked';run['errors'].append({'companyId':'','message':str(error)[:300]})
    except BudgetReached as error:
        run['outcome']='partial';run['warnings'].append(str(error))
    finally:
        source.close()
    if run['outcome']=='complete' and (run['warnings'] or run['errors']):run['outcome']='partial' if run['checked'] or all_items else 'failed'
    emit(final=True)
    return {'items':all_items,'run':run}
