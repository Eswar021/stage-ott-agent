"""HTTP integration checks; all inference is local and keyless."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from ott_agent.review import page_view
from ott_agent.service import Service
from ott_agent.store import Store
from ott_agent.util import UTC, iso, stable_id, ts
from ott_agent.web import create_app
from test_domain import NOW, event
from test_engine import CountingProvider, mini_pack


def wait_job(client, jid):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        job = client.get('/jobs/' + jid).json()
        if job['status'] not in {'QUEUED', 'RUNNING'}:
            assert job['status'] == 'SUCCEEDED', job
            return job
        time.sleep(.005)
    pytest.fail('Local job did not finish')


def live_pack():
    p = mini_pack()
    now = datetime.now(UTC)
    p.replay_start = now - timedelta(seconds=1)
    profile = p.profiles['U-test']
    profile.update(signup_at=iso(now - timedelta(hours=3)), updated_at=iso(now - timedelta(minutes=2)), snapshot_at=iso(now - timedelta(minutes=1)))
    for title in p.catalogue.values():
        title['release_date'] = now.date().isoformat()
    return p


def payload(eid='http-open', kind='app_open', **extra):
    now = iso(datetime.now(UTC))
    return {'event_id':eid, 'user_id':'U-test', 'event':kind, 'occurred_at':now, 'sent_at':now,
            'app_dialect':'d2', 'app_language':'hin', **extra}


def test_dashboard_docs_schema_and_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-secret-must-not-leak')
    with TestClient(create_app(project=tmp_path, pack=mini_pack())) as c:
        assert c.get('/').status_code == c.get('/assets/app.js').status_code == 200
        assert c.get('/docs').status_code == 200
        schema = c.get('/openapi.json').json()
        assert {'/events','/decisions/{decision_id}','/replays','/viewers/{viewer_id}/home'} <= schema['paths'].keys()
        response = c.get('/config')
        assert response.json()['openai_available']
        assert 'unit-test-secret' not in response.text
        assert c.get('/viewers/U-test/home').json()['status'] == 'DEFAULT'


def test_source_demo_real_engine_audit_and_retry_do_not_duplicate_calls(tmp_path):
    provider = CountingProvider()
    with TestClient(create_app(project=tmp_path, pack=mini_pack(), provider_factory=lambda _:provider)) as c:
        request = {'trigger_event_id':'trigger','provider':'replay'}
        first = c.post('/demo/decisions',json=request,headers={'Idempotency-Key':'same-demo'})
        assert first.status_code == 202
        job = wait_job(c, first.json()['job_id'])
        second = c.post('/demo/decisions',json=request,headers={'Idempotency-Key':'same-demo'})
        assert second.json()['job_id'] == job['job_id']
        assert provider.calls == 1
        result=job['result'];q={'run':result['run_id']}
        detail=c.get('/decisions/'+result['decision_id'],params=q).json()
        assert detail['summary']['status']=='PUBLISHED'
        assert detail['summary']['content']
        assert detail['actions'][0]['decision_id']==result['decision_id']
        assert detail['home']['status']=='APPROVED'
        assert c.get('/decisions',params=q).json()['total']==1
        assert c.get('/viewers/U-test',params=q).json()['active_goal']
        assert c.get('/reports',params=q).json()['impact_report'] is None
        assert c.post('/demo/decisions',json={'trigger_event_id':'trigger','provider':'openai'},headers={'Idempotency-Key':'same-demo'}).status_code==409


def test_live_single_event_server_receipt_dedup_and_state_only_update(tmp_path):
    provider=CountingProvider()
    with TestClient(create_app(project=tmp_path,pack=live_pack(),provider_factory=lambda _:provider)) as c:
        body=payload()
        before=datetime.now(UTC)
        response=c.post('/events',json=body)
        job=wait_job(c,response.json()['job_id']);r=job['result']
        assert r['mode']=='live_events'
        detail=c.get('/decisions/'+r['decision_id'],params={'run':r['run_id']}).json()
        assert ts(detail['summary']['trigger']['received_at'])>=before-timedelta(milliseconds=2)
        assert detail['audit']['context']['facts']['observation_coverage']['lifetime_from_signup'] is False
        assert provider.calls==1
        assert c.post('/events',json=body).json()['job_id']==job['job_id']
        assert c.post('/events',json={**body,'app_dialect':'d1'}).status_code==409
        optout=c.post('/events',json=payload('optout','notification_opt_out',properties={'channel':'push'}))
        updated=wait_job(c,optout.json()['job_id'])
        assert updated['result']['decision_id'] is None
        assert provider.calls==1
        viewer=c.get('/viewers/U-test',params={'run':'live:U-test'}).json()
        assert viewer['facts']['notifications_opt_in'] is False
        assert c.get('/viewers/U-test/home').json()['status']=='DEFAULT'
        assert c.get('/viewers/U-test/home',params={'at':iso(NOW)}).status_code==422


def test_inflight_optout_applies_before_publication_and_same_viewer_serializes(tmp_path):
    entered,release=threading.Event(),threading.Event()
    class Blocking(CountingProvider):
        def plan(self,*args,**kwargs):
            entered.set()
            assert release.wait(3)
            return super().plan(*args,**kwargs)
    provider=Blocking()
    with TestClient(create_app(project=tmp_path,pack=live_pack(),provider_factory=lambda _:provider)) as c:
        started=c.post('/events',json=payload()).json()
        assert entered.wait(2)
        optout=c.post('/events',json=payload('during-call','notification_opt_out',properties={'channel':'push'})).json()
        # HTTP acceptance is independent of the worker's provider call.
        assert optout['status'] in {'QUEUED','RUNNING'}
        release.set()
        first=wait_job(c,started['job_id']);wait_job(c,optout['job_id'])
        result=first['result']
        detail=c.get('/decisions/'+result['decision_id'],params={'run':result['run_id']}).json()
        assert detail['audit']['state_changed_during_inference']
        assert detail['audit']['validation_context']['facts']['notifications_opt_in'] is False


def test_cross_viewer_threads_can_make_progress_concurrently(tmp_path):
    p=live_pack()
    p.profiles['U-other']={**deepcopy(p.profiles['U-test']),'user_id':'U-other'}
    p.arms['U-other']='agent'
    both=threading.Barrier(2)
    class Parallel(CountingProvider):
        def plan(self,*args,**kwargs):
            both.wait(2)
            return super().plan(*args,**kwargs)
    with TestClient(create_app(project=tmp_path,pack=p,provider_factory=lambda _:Parallel())) as c:
        a=c.post('/events',json=payload('parallel-a')).json()
        b=c.post('/events',json={**payload('parallel-b'),'user_id':'U-other'}).json()
        wait_job(c,a['job_id']);wait_job(c,b['job_id'])


def test_read_time_withdrawal_and_ttl_dialect_guards(tmp_path):
    p=live_pack()
    with TestClient(create_app(project=tmp_path,pack=p)) as c:
        started=c.post('/events',json=payload()).json()
        result=wait_job(c,started['job_id'])['result']
        page=c.get('/viewers/U-test/home').json()
        assert page['status']=='APPROVED'
        tid=page['page']['hero_title_id']
        assert c.post('/controls/withdrawals',json={'title_id':tid}).status_code==200
        assert c.get('/viewers/U-test/home').json()['reason']=='content_unavailable_or_withdrawn'
        # Publication record is preserved; serving refuses it immediately.
        detail=c.get('/decisions/'+result['decision_id'],params={'run':result['run_id']}).json()
        assert detail['actions'][0]['hero_title_id']==tid
        action=page['page'];published=ts(action['published_at'])
        assert page_view(p,[action],now=published+timedelta(minutes=120),dialect='d2')['reason']=='page_expired'
        assert page_view(p,[action],now=published,dialect='d1')['reason']=='dialect_changed_or_unknown'


def test_holdout_and_buffered_http_triggers_never_invoke_model(tmp_path):
    p=live_pack();p.arms['U-test']='holdout';provider=CountingProvider()
    with TestClient(create_app(project=tmp_path,pack=p,provider_factory=lambda _:provider)) as c:
        job=wait_job(c,c.post('/events',json=payload()).json()['job_id'])
        result=job['result'];detail=c.get('/decisions/'+result['decision_id'],params={'run':result['run_id']}).json()
        assert detail['summary']['no_action_reason']=='holdout'
        assert provider.calls==0


def test_validation_and_local_mutation_guard(tmp_path):
    with TestClient(create_app(project=tmp_path,pack=mini_pack())) as c:
        assert c.post('/events',json={**payload(),'received_at':iso(NOW)}).status_code==422
        assert c.post('/events',json=payload(),headers={'Origin':'https://unrelated.example'}).status_code==403
        assert c.post('/events',content='{}',headers={'Content-Type':'text/plain'}).status_code==415
        assert c.post('/events',content='x'*65537,headers={'Content-Type':'application/json'}).status_code==413
        assert c.post('/events',json=payload('bad-date',occurred_at='no-date')).status_code==422
        assert c.post('/events',json=payload('bad-billing','purchase_completed')).status_code==422
        assert c.post('/events',json=payload('bad-title','play_started',properties={'title_id':'unknown'})).status_code==422
        assert c.post('/events',json={**payload(),'user_id':'U-unknown'}).status_code==422
        assert c.post('/demo/decisions',json={'trigger_event_id':'unknown'}).status_code==422
        assert c.get('/decisions',params={'run':'../../.env'}).status_code==404


def test_restart_recovers_queued_event_and_retains_idempotency(tmp_path):
    p=live_pack();provider=CountingProvider()
    app=create_app(project=tmp_path,pack=p,provider_factory=lambda _:provider)
    with TestClient(app) as c:
        # Persist accepted work without scheduling, as if a process died at 202.
        original=app.state.service._schedule
        app.state.service._schedule=lambda jid:None
        body=payload('restart-event')
        job=c.post('/events',json=body).json()
        assert job['status']=='QUEUED'
        app.state.service._schedule=original
    with TestClient(create_app(project=tmp_path,pack=p,provider_factory=lambda _:provider)) as c:
        done=wait_job(c,job['job_id'])
        assert provider.calls==1
        assert c.post('/events',json=body).json()['job_id']==done['job_id']
        assert c.get('/decisions',params={'run':'live:U-test'}).json()['total']==1


def test_single_runtime_owner_and_readonly_jsonl_run(tmp_path):
    p=mini_pack();service=Service(tmp_path,p)
    try:
        with pytest.raises(RuntimeError,match='already has an owner'):
            Service(tmp_path,p)
    finally:
        service.close()
    # Portable curated runs can be reviewed without shipping their SQLite DB.
    from ott_agent.engine import Engine
    from ott_agent.cli import _export
    folder=tmp_path/'sample_run';store=Store(folder/'state.sqlite3')
    Engine(p,store).run();_export(store,folder,p);store.close()
    (folder/'state.sqlite3').unlink()
    (folder/'run_meta.json').write_text(json.dumps({'provider':'replay'}))
    with TestClient(create_app(project=tmp_path,pack=p)) as c:
        summaries=c.get('/decisions').json()
        assert summaries['total']==1
        detail=c.get('/decisions/'+summaries['items'][0]['decision_id']).json()
        assert detail['summary']['status']=='PUBLISHED'
        assert detail['home']['status']=='APPROVED'


def test_explicit_failed_job_retry_resumes_pending_without_duplicate_action(tmp_path):
    class Once(CountingProvider):
        broken = True
        def plan(self,*args,**kwargs):
            if self.broken:
                self.broken=False
                raise RuntimeError('internal-value-that-must-not-leak')
            return super().plan(*args,**kwargs)
    provider=Once()
    with TestClient(create_app(project=tmp_path,pack=live_pack(),provider_factory=lambda _:provider)) as c:
        first=c.post('/events',json=payload('retry-me')).json()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            failed=c.get('/jobs/'+first['job_id']).json()
            if failed['status']=='FAILED':break
            time.sleep(.005)
        assert failed['status']=='FAILED'
        assert 'internal-value' not in failed['error']
        retry=c.post('/jobs/'+first['job_id']+'/retry',json={})
        assert retry.status_code==202
        result=wait_job(c,first['job_id'])['result']
        assert c.get('/decisions',params={'run':result['run_id']}).json()['total']==1
        assert provider.calls==1
        assert c.post('/jobs/'+first['job_id']+'/retry',json={}).status_code==409


def test_same_viewer_second_trigger_observes_only_committed_plan(tmp_path):
    entered,release=threading.Event(),threading.Event()
    class Blocking(CountingProvider):
        def plan(self,*args,**kwargs):
            entered.set();assert release.wait(3)
            return super().plan(*args,**kwargs)
    provider=Blocking()
    with TestClient(create_app(project=tmp_path,pack=live_pack(),provider_factory=lambda _:provider)) as c:
        first=c.post('/events',json=payload('owner-first')).json()
        assert entered.wait(2)
        second=c.post('/events',json=payload('owner-second')).json()
        release.set()
        a=wait_job(c,first['job_id'])['result'];b=wait_job(c,second['job_id'])['result']
        detail=c.get('/decisions/'+b['decision_id'],params={'run':b['run_id']}).json()
        context=detail['audit']['context']
        assert context['active_goal']
        assert ts(context['active_goal']['started_at'])<=ts(context['as_of'])
        assert c.get('/decisions',params={'run':a['run_id']}).json()['total']==2


def test_live_goal_missing_signal_is_inconclusive_without_watermark(tmp_path):
    from ott_agent.engine import Engine
    p=mini_pack();p.incomplete_live_coverage=True
    store=Store(tmp_path/'unknown.sqlite3')
    engine=Engine(p,store);engine.run()
    goal=store.open_goal('U-test');assert goal
    with store.transaction():
        engine._reconcile('U-test',ts(goal['deadline'])+timedelta(seconds=1))
    assert store.goals()[0]['status']=='inconclusive'
    assert store.open_goal('U-test') is None
    store.close()


def test_event_already_in_source_history_returns_auditable_duplicate_decision(tmp_path):
    p=live_pack();p.history=[event('already-imported')];provider=CountingProvider()
    with TestClient(create_app(project=tmp_path,pack=p,provider_factory=lambda _:provider)) as c:
        job=wait_job(c,c.post('/events',json=payload('already-imported')).json()['job_id'])
        result=job['result'];assert result['decision_id']
        detail=c.get('/decisions/'+result['decision_id'],params={'run':result['run_id']}).json()
        assert detail['summary']['no_action_reason']=='duplicate_trigger'
        assert detail['summary']['content']==[]
        assert provider.calls==0


def test_final_publication_and_inbox_acceptance_have_atomic_knowledge_boundary(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import ott_agent.engine as module
    original=module.verify_proposal
    checking,release,attempted=threading.Event(),threading.Event(),threading.Event()
    def slow_check(*args,**kwargs):
        checking.set();assert release.wait(3)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'verify_proposal',slow_check)
    app=create_app(project=tmp_path,pack=live_pack())
    with TestClient(app) as c:
        first=c.post('/events',json=payload('boundary-open')).json()
        assert checking.wait(2)
        original_submit=app.state.service.submit
        def submitting(kind,request,**kwargs):
            attempted.set()
            return original_submit(kind,request,**kwargs)
        monkeypatch.setattr(app.state.service,'submit',submitting)
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(c.post,'/events',json=payload('boundary-optout','notification_opt_out',properties={'channel':'push'}))
            assert attempted.wait(2)
            # Acceptance cannot cross an unrefreshed final verification/commit.
            assert not future.done()
            release.set()
            later=future.result(timeout=3)
        a=wait_job(c,first['job_id'])['result'];wait_job(c,later.json()['job_id'])
        decision=c.get('/decisions/'+a['decision_id'],params={'run':a['run_id']}).json()['audit']
        accepted=next(e for e in app.state.service.incoming('U-test') if e['event_id']=='boundary-optout')
        assert ts(accepted['received_at'])>=ts(decision['published_at'])
        assert c.get('/viewers/U-test/home').json()['status']=='DEFAULT'


def test_live_form_examples_are_grounded_readonly_and_accepted_by_event_api(tmp_path):
    p=live_pack();provider=CountingProvider()
    kinds=['app_open','login_success','play_started','episode_completed','purchase_completed','notification_opt_out','app_dialect_changed']
    with TestClient(create_app(project=tmp_path,pack=p,provider_factory=lambda _:provider)) as c:
        examples={}
        for kind in kinds:
            r=c.get('/events/examples',params={'event_type':kind,'dialect':'d2'})
            assert r.status_code==200
            example=examples[kind]=r.json()
            assert example['hint'] and not example['records_event'] and not example['calls_model']
        assert provider.calls==0 and c.get('/jobs').json()==[]
        assert examples['app_open']['properties']==examples['login_success']['properties']=={}
        for kind in ['play_started','episode_completed']:
            props=examples[kind]['properties']
            title=p.catalogue[props['title_id']]
            assert title['dialect']=='d2'
            assert props['episode_id'] in {ep['episode_id'] for ep in p.episodes[title['content_slug']]}
        for kind in kinds:
            response=c.post('/events',json=payload('example-'+kind,kind,properties=examples[kind]['properties']))
            assert response.status_code==202,response.text
            wait_job(c,response.json()['job_id'])
        # The source snapshot is not edited by either examples or submissions.
        assert p.profiles['U-test']['subscription_status']=='never'
        viewer=c.get('/viewers/U-test',params={'run':'live:U-test'}).json()
        assert viewer['facts']['subscription_status']=='active'
        assert viewer['facts']['notifications_opt_in'] is False


def test_missing_playback_properties_and_conflicting_dialect_change_are_rejected(tmp_path):
    with TestClient(create_app(project=tmp_path,pack=live_pack())) as c:
        for kind in ['play_started','episode_completed']:
            assert c.post('/events',json=payload('missing-'+kind,kind)).status_code==422
            response=c.post('/events',json=payload('missing-episode-'+kind,kind,properties={'title_id':'T01'}))
            assert response.status_code==422 and 'episode_id' in response.json()['detail']
        assert c.post('/events',json=payload('conflicting-dialect','app_dialect_changed',properties={'to':'d1'})).status_code==422
        assert c.get('/events/examples',params={'event_type':'play_started','dialect':'d3'}).status_code==422
