'use strict';
const $ = id => document.getElementById(id);
const state = {run:'sample_run', runs:[], offset:0, limit:25, total:0, selected:null, detail:null, tab:'experience', mode:'source', config:{}, screen:'decisions', generation:0};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const human = value => String(value ?? '').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
const date = value => value ? new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'Asia/Kolkata'})+' IST' : '—';
const n = value => Number(value ?? 0).toLocaleString();
const money = value => '$'+Number(value ?? 0).toFixed(5);
const pretty = value => esc(JSON.stringify(value,null,2));
const runQuery = () => 'run='+encodeURIComponent(state.run);
function error(message){$('error').textContent=message;$('error').hidden=!message;}
function notice(message){$('notice').textContent=message;$('notice').hidden=!message;}
async function api(path, options={}) {
  const response = await fetch(path,options.body ? {...options,headers:{'Content-Type':'application/json',...options.headers},body:JSON.stringify(options.body)} : options);
  let data; try{data=await response.json();}catch{throw new Error('The server returned an unreadable response.');}
  if(!response.ok){const detail=data.detail;throw new Error(Array.isArray(detail)?detail.map(d=>d.msg).join('; '):detail || 'Request failed.');}
  return data;
}
function providerBadge(d){const provider=d.provider||'unknown';const label=provider==='openai'?'OpenAI':provider==='replay'?'Offline replay':provider==='gate'?'Gate · no call':human(provider);return `<span class="pill ${provider==='openai'?'openai':provider==='replay'?'replay':'gate'}">${esc(label)}</span>${d.fallback_reason?'<span class="pill warning">Fallback</span>':''}`;}
function setScreen(screen){state.screen=screen;document.querySelectorAll('.screen').forEach(el=>el.hidden=el.id!=='screen-'+screen);document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.screen===screen));$('screen-title').textContent=({decisions:'Decision explorer',playground:'Agent playground',reports:'Evaluation overview',jobs:'Background jobs'})[screen];error('');if(screen==='reports')loadReports().catch(e=>error(e.message));if(screen==='jobs')loadJobs().catch(e=>error(e.message));}
async function loadRuns(preferred=null){
  state.runs=await api('/runs');
  const ids=state.runs.map(r=>r.run_id);
  if(preferred&&ids.includes(preferred))state.run=preferred;
  else if(!ids.includes(state.run))state.run=ids[0]||'';
  $('run-select').innerHTML=state.runs.map(r=>{const meta=r.metadata;const label=r.run_id.startsWith('demo:')?'Sandbox · '+(meta.viewer_id||r.run_id.slice(-10)):r.run_id.startsWith('live:')?'Live events · '+r.run_id.slice(5):r.run_id;return `<option value="${esc(r.run_id)}">${esc(label)}${meta.provider==='openai'?' · Hybrid OpenAI':meta.provider==='replay'?' · Offline':''}</option>`;}).join('');
  $('run-select').value=state.run;
}
async function loadMetrics(){
  const run=state.runs.find(r=>r.run_id===state.run)||{};
  let report=run.report||{},meta=run.metadata||{};
  const diagnostics=report.diagnostics||{};const latency=report.latency||{};
  const decisionCounts=diagnostics.decisions_by_status||{};
  const total=Object.values(decisionCounts).reduce((a,b)=>a+b,0);
  const published=decisionCounts.PUBLISHED;
  const counts=meta.provider_decision_counts||{};
  const live=latency.live_openai||{};
  const spend=meta.provider_summary||meta.provider_spend||{};
  const metrics=[['Recorded decisions',meta.decision_count??(total||state.total),'Saved results in this workspace'],['Published decisions',published??meta.published_decisions??'—','Approved actions, not goal successes'],['OpenAI planning attempts',live.planning_attempts??counts.openai??meta.provider_attempts??'—',meta.provider==='openai'?'Sampled live calls; remaining decisions use gates/fallback':meta.mode==='source_trigger_sandbox'?'Isolated trigger; no simulated outcomes':'Offline results are labelled explicitly'],['Accounted model cost',money(spend.run_accounted_usd),meta.mode==='live_events'?'Local ledger · all jobs for this viewer':'Conservative local ledger · this run']];
  $('metrics').innerHTML=metrics.map(([label,value,note])=>`<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value">${typeof value==='number'?n(value):esc(value)}</div><div class="metric-note">${esc(note)}</div></div>`).join('');
}
async function loadDecisions(select=null){
  const generation=++state.generation;
  error('');
  if(!state.run){$('decision-items').innerHTML='<div class="empty">No saved runs yet. Create a decision in Playground.</div>';return;}
  const params=new URLSearchParams({run:state.run,offset:state.offset,limit:state.limit,search:$('search').value,provider:$('provider-filter').value,status:$('status-filter').value});
  const data=await api('/decisions?'+params);
  if(generation!==state.generation)return;
  state.total=data.total;$('decision-count').textContent=n(data.total);
  $('decision-items').innerHTML=data.items.map(d=>`<button class="decision-card ${d.decision_id===state.selected?'selected':''}" data-decision="${esc(d.decision_id)}"><div class="card-top"><span class="viewer-name">${esc(d.viewer_id)}</span>${providerBadge({...d,fallback_reason:null})}</div><div class="card-goal">${d.goal?esc(d.goal.id+' · '+human(d.goal.name||'Active goal')):'No goal committed'}</div><div class="card-bottom"><span>${d.status==='PUBLISHED'?'● Approved action':'○ No action'}${d.fallback_reason?' · fallback':''}</span><span>${Number(d.decided_in_ms).toFixed(0)} ms</span></div></button>`).join('') || '<div class="empty">No decisions match these filters.<small>Try another provider or viewer.</small></div>';
  $('previous').disabled=state.offset===0;$('next').disabled=state.offset+state.limit>=data.total;
  $('page-label').textContent=data.total?`${state.offset+1}–${Math.min(state.offset+state.limit,data.total)} / ${n(data.total)}`:'0 results';
  await loadMetrics();
  const chosen=select||data.items.find(d=>d.decision_id===state.selected)?.decision_id||data.items.find(d=>d.status==='PUBLISHED')?.decision_id||data.items[0]?.decision_id;
  if(chosen)await selectDecision(chosen);else{$('decision-detail').innerHTML='<div class="empty large">Select another filter to find decisions.</div>';state.selected=null;}
}
async function selectDecision(id){
  const run=state.run;state.selected=id;
  document.querySelectorAll('[data-decision]').forEach(el=>el.classList.toggle('selected',el.dataset.decision===id));
  const result=await api('/decisions/'+encodeURIComponent(id)+'?'+runQuery());
  if(state.selected!==id||state.run!==run)return;
  state.detail=result;state.tab='experience';renderDetail();
}
function renderDetail(){
  const {summary:d,audit,viewer}=state.detail;
  const facts=(audit.validation_context||audit.context||{}).facts||{};
  const goal=d.goal;
  $('decision-detail').innerHTML=`<div class="detail-head"><div class="detail-kicker">${providerBadge(d)}<span class="pill ${d.status==='PUBLISHED'?'outline':'gate'}">${esc(human(d.status))}</span></div><div class="detail-title"><div><h2>${esc(d.viewer_id)} <span style="color:#a0a596;font-weight:400">/ ${esc(human(d.trigger.event))}</span></h2><p class="detail-id">${esc(d.decision_id)} · ${date(d.trigger.deliver_at)}</p></div><span class="pill outline">${Number(d.decided_in_ms).toFixed(0)} ms</span></div><p class="detail-reason">${esc(audit.proposal?.reason_summary||d.no_action_reason||'No proposal recorded.')}</p><div class="decision-facts"><div class="fact-tile"><label>Goal</label><strong>${goal?esc(goal.id+' · '+human(goal.name||goal.id)):'No commitment'}</strong><small>${goal?'Deadline '+date(goal.deadline):'The agent abstained safely.'}</small></div><div class="fact-tile"><label>Dialect</label><strong>${esc(d.dialect.used||'Unknown')} · ${esc(human(d.dialect.source||'No current source'))}</strong><small>${d.dialect.conflicts?.length?n(d.dialect.conflicts.length)+' source conflict(s) preserved':'No recorded source conflicts'}</small></div><div class="fact-tile"><label>Confidence</label><strong>${esc(human(d.confidence?.level||'Unknown'))}</strong><small>${esc(d.confidence?.reason||'No confidence supplied.')}</small></div></div></div><div class="tabs" role="tablist" aria-label="Decision views">${[['experience','Viewer experience'],['evidence','Evidence & goal'],['model','Model & checks'],['json','Decision JSON']].map(([id,label])=>`<button class="tab ${id===state.tab?'selected':''}" role="tab" aria-selected="${id===state.tab}" data-tab="${id}">${label}</button>`).join('')}</div><div class="tab-content" id="detail-content" role="tabpanel"></div>`;
  renderTab();
}
function titleName(tid){return state.detail.titles[tid]?.title||tid||'No hero';}
function previewPage(page){
  if(!page)return `<div class="subpanel"><h3>No homepage published for this decision</h3><p>${esc(state.detail.summary.no_action_reason||'This decision did not publish a homepage. Other approved action types appear below.')}</p></div>`;
  const hero=state.detail.titles[page.hero_title_id]||{};
  return `<div class="preview"><div class="preview-nav"><b>STAGE</b><span>FOR YOU · ${esc(page.dialect)}</span></div><div class="hero"><div class="section-label">SELECTED FOR THIS VIEWER</div><h3>${esc(titleName(page.hero_title_id))}</h3><p>${esc(human(hero.content_type||'Title'))} · ${esc((hero.genres||'').replaceAll('|',' · '))} · ${esc(page.hero_title_id)}</p><span class="preview-button">▶ Watch in OTT app</span></div>${(page.rows||[]).map(row=>`<div class="preview-row"><h3>${esc(human(row.row_type))}</h3><div class="posters">${row.title_ids.map(tid=>`<div class="poster"><strong>${esc(titleName(tid))}</strong><small>${esc(tid)} · ${esc(state.detail.titles[tid]?.dialect||page.dialect)}</small></div>`).join('')}</div></div>`).join('')}</div>`;
}
function outcomeBlock(){
  const {summary:d,viewer}=state.detail;
  const goal=(viewer?.goal_history||[]).find(g=>g.goal_instance_id===d.goal?.goal_instance_id);
  const status=goal?.judgment?.status||goal?.status;
  const text=status?`Observed goal status: ${human(status)}. ${goal.judgment?.reason||'See the goal history for supporting evidence.'}`:'No later goal judgment is available in this workspace. A published action alone does not establish success.';
  return `<div class="outcome"><span class="outcome-icon">◎</span><div><h3>Publication and outcome are separate</h3><p>${esc(text)}</p>${status?'<p>Recorded replay outcomes come from the supplied simulator; they are not real customer behavior.</p>':''}</div></div>`;
}
function renderTab(){
  const {summary:d,audit,actions,viewer}=state.detail;const container=$('detail-content');
  document.querySelectorAll('[data-tab]').forEach(el=>{el.classList.toggle('selected',el.dataset.tab===state.tab);el.setAttribute('aria-selected',el.dataset.tab===state.tab);});
  if(state.tab==='experience'){
    const page=actions.find(a=>a.type==='home_page');
    container.innerHTML=`<div class="section-label">APPROVED OUTPUT / PUBLICATION SNAPSHOT</div>${previewPage(page)}<p class="preview-caption">Content and layout come only from approved actions. This is a visual preview with source title names, not a playable OTT frontend or a claim of current availability.</p>${actions.filter(a=>a.type!=='home_page').map(a=>`<div class="message-preview"><span class="pill">${esc(human(a.type))}</span><h3 style="margin-top:10px">${esc(a.text_title||titleName(a.title_id))}</h3><p>${esc(a.text_body||a.template_id||'Approved intervention')}</p></div>`).join('')}${d.fallback_reason?`<div class="subpanel"><h3>Fallback was used</h3><p>${esc(human(d.fallback_reason))}. The page above shows the independently approved result.</p></div>`:''}${d.repair?`<div class="subpanel"><h3>The verifier adjusted the layout</h3><p>${esc(d.repair)}</p></div>`:''}${outcomeBlock()}`;
  }else if(state.tab==='evidence'){
    const ctx=audit.validation_context||audit.context||{};const facts=ctx.facts||{};const idx={...(audit.context?.evidence_index||{}),...(ctx.evidence_index||{}),...(audit.gate_evidence||{})};
    container.innerHTML=`<div class="section-label">EVIDENCE CITED BY THIS DECISION</div>${d.evidence.map(ref=>`<div class="evidence-row"><code>${esc(ref)}</code><span>${esc(human(idx[ref]?.source||ref.split(':')[0]))}${idx[ref]?.known_at?' · known '+date(idx[ref].known_at):''}</span></div>`).join('')}<div class="subpanel"><h3>Resolved viewer facts</h3>${[['Subscription',facts.subscription_status],['Ever paid',facts.ever_paid===null?'Unknown':facts.ever_paid],['Observed plays',facts.behavior?.play_count],['Observed completions',facts.behavior?.completion_count],['Observation starts',facts.observation_coverage?.start]].map(([k,v])=>`<div class="key-value"><span>${k}</span><b>${esc(v??'Unknown')}</b></div>`).join('')}</div>${d.dialect.conflicts?.length?`<details open><summary>Dialect conflicts and source precedence</summary><pre>${pretty(d.dialect)}</pre></details>`:''}<details><summary>Goal options and eligibility reasons</summary><pre>${pretty(ctx.goal_assessments||[])}</pre></details><details><summary>Observation coverage, assumptions and uncertainties</summary><pre>${pretty({coverage:facts.observation_coverage,assumptions:d.assumptions,uncertainties:d.uncertainties})}</pre></details><div class="section-label" style="margin-top:25px">VIEWER GOAL HISTORY / END OF WORKSPACE</div><div class="goal-timeline">${(viewer?.goal_history||[]).map(g=>`<div class="goal-step"><strong>${esc(g.id)}</strong><span class="pill ${g.status==='succeeded'?'':'outline'}">${esc(human(g.status))}</span><p>${date(g.started_at)} → ${date(g.deadline)}</p><p>${esc(g.judgment?.reason||g.reason_summary||'No outcome judgment recorded.')}</p></div>`).join('')||'<p class="muted">No goal history recorded.</p>'}</div>`;
  }else if(state.tab==='model'){
    const result=audit.provider_result||{};
    container.innerHTML=`<div class="section-label">PROVIDER TRACE</div><div class="key-value"><span>Planner used</span><b>${esc(result.provider)}${result.model?' / '+esc(result.model):''}</b></div><div class="key-value"><span>Model requests / total logical tool calls</span><b>${audit.model_calls} / ${audit.tool_calls}</b></div><div class="key-value"><span>Evidence changed during inference</span><b>${audit.state_changed_during_inference?'Yes · revalidated before publication':'No recorded change'}</b></div><p class="inline-note">${result.provider==='openai'?'This record contains an actual OpenAI provider result. The runtime independently approved, repaired or replaced its proposal.':'This record is explicitly offline or gated. It is not a real OpenAI response.'}</p><div class="section-label" style="margin-top:24px">INDEPENDENT PUBLICATION CHECKS</div><div class="check-list">${d.checks.passed.map(c=>`<span class="check">${esc(human(c))}</span>`).join('')}${d.checks.failed.map(c=>`<span class="check bad">${esc(human(c))}</span>`).join('')}</div>${d.checks.rejected_proposal_checks?.length?`<div class="subpanel"><h3>Original proposal checks that failed</h3><div class="check-list">${d.checks.rejected_proposal_checks.map(c=>`<span class="check bad">${esc(human(c))}</span>`).join('')}</div><p>${esc(d.repair||d.fallback_reason||'The rejected proposal was not published.')}</p></div>`:''}<details open><summary>Model/provider proposal before independent checks</summary><pre>${pretty(result.proposal)}</pre></details><details><summary>Raw provider response and tool trace</summary><pre>${pretty(result)}</pre></details><details><summary>Context sent to planning (runtime evidence manifest)</summary><pre>${pretty(audit.context)}</pre></details>`;
  }else{
    container.innerHTML=`<div class="json-actions"><button id="copy-json" class="secondary">Copy JSON</button><button id="download-json" class="secondary">Download JSON ↓</button></div><p class="inline-note">Required decision structure, projected from verified publication records. Confidence stays qualitative; no calibrated probability is invented.</p><pre>${pretty(d)}</pre><details><summary>Complete decision audit</summary><pre>${pretty(audit)}</pre></details>`;
    $('copy-json').onclick=async()=>{try{await navigator.clipboard.writeText(JSON.stringify(d,null,2));$('copy-json').textContent='Copied ✓';}catch{error('Clipboard unavailable. Use Download JSON instead.');}};
    $('download-json').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(d,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=d.decision_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  }
}
async function loadTriggers(){const data=await api('/demo/triggers?'+new URLSearchParams({viewer:$('trigger-search').value,limit:100}));$('trigger-select').innerHTML=data.items.map(t=>`<option value="${esc(t.event_id)}">${esc(t.user_id)} · ${esc(t.event)} · ${esc(t.app_dialect)} · ${date(t.deliver_at)} · ${esc(t.arm)}</option>`).join('')||'<option value="">No matching supplied events</option>';}
function renderActivity(job){
  const result=job.result||{};const running=['QUEUED','RUNNING'].includes(job.status);
  $('play-activity').className='activity';
  $('play-activity').innerHTML=`<span class="pill ${job.status==='FAILED'?'danger':running?'warning':''}">${running?'<span class="loader"></span>':''}${esc(human(job.status))}</span><h3>${running?'Planning and verifying…':job.status==='SUCCEEDED'?(result.decision_id?'Decision ready':'Viewer state updated'):'Job could not complete'}</h3><p>${esc(job.error||result.note||'The backend is processing the event. You can continue browsing saved results.')}</p><div class="job-id">${esc(job.job_id)}</div>${result.provider_spend?`<p>Accounted inference cost: ${money(result.provider_spend.run_accounted_usd)}</p>`:''}${result.run_id?`<button class="primary" id="open-job-result">${result.decision_id?'Explore decision':'Open workspace'} ↗</button>`:''}${job.status==='SUCCEEDED'&&!result.decision_id?'<p>State-only events do not need a new LLM decision.</p>':''}`;
  if($('open-job-result'))$('open-job-result').onclick=()=>openResult(result);
}
async function followJob(id){
  const job=await api('/jobs/'+encodeURIComponent(id));renderActivity(job);
  if(['QUEUED','RUNNING'].includes(job.status)){await new Promise(resolve=>setTimeout(resolve,900));return followJob(id);}
  return job;
}
async function openResult(result){
  await loadRuns(result.run_id);$('provider-filter').value='';$('status-filter').value='';$('search').value='';state.offset=0;state.selected=null;setScreen('decisions');
  if(result.decision_id){await loadDecisions(result.decision_id);}else{
    await loadDecisions();if(!result.viewer_id)return;const viewer=await api('/viewers/'+encodeURIComponent(result.viewer_id)+'?'+runQuery());
    $('decision-detail').innerHTML=`<div class="detail-head"><div class="section-label">VIEWER MEMORY / LIVE EVENTS</div><h2>${esc(result.viewer_id)}</h2><p class="detail-reason">The event updated facts and goal progress. No new model decision was needed.</p><pre>${pretty(viewer)}</pre></div>`;
  }
}
async function runDecision(){
  error('');const button=$('run-decision');button.disabled=true;
  try{
    const provider=$('demo-provider').value;let path,body;
    if(state.mode==='source'){path='/demo/decisions';body={provider,trigger_event_id:$('trigger-select').value};if(!body.trigger_event_id)throw new Error('Choose a supplied trigger first.');}
    else{if(state.exampleLoading)throw new Error('Wait for the event example to finish loading.');path='/events';const now=new Date().toISOString();body={event_id:'web-'+crypto.randomUUID(),user_id:$('live-viewer').value.trim(),event:$('live-kind').value,session_id:'studio-session',occurred_at:now,sent_at:now,app_dialect:$('live-dialect').value,app_language:$('live-language').value,properties:JSON.parse($('live-properties').value),provider};}
    const job=await api(path,{method:'POST',body});renderActivity(job);await followJob(job.job_id);
  }catch(e){error(e.message);}finally{button.disabled=false;}
}
async function loadReports(){
  if(!state.run)return;
  const reports=await api('/reports?'+runQuery());
  const meta=reports.run_meta||{};const latency=reports.latency_report||{};const impact=reports.impact_report||{};const validation=reports.validation_report||{};
  if(!reports.report_summary){$('report-content').innerHTML='<div class="panel empty">This workspace contains interactive events, not a full evaluation.<small>Run a full replay below to produce simulator outcomes, validation, latency and randomized-impact reports.</small></div>';return;}
  const diagnostic=reports.report_summary.diagnostics||{};
  const metrics=Object.entries(impact.metrics||{});
  $('report-content').innerHTML=`<div class="notice">${meta.provider==='openai'?'Hybrid policy: only a bounded sample used OpenAI; the rest used gates or deterministic fallback. Impact estimates describe this mixed policy.':'Offline replay: these results evaluate the deterministic planner and supplied simulator, not OpenAI quality or production traffic.'}</div><div class="report-grid"><section class="panel report-block"><div class="section-label">RANDOMIZED COMPARISON</div><h2>Assignment-level outcomes</h2><p class="muted">Agent <span style="color:var(--green)">●</span> / holdout <span style="color:#a5ae97">●</span>. Same anchored windows and assignment denominators.</p>${metrics.map(([name,m])=>{const a=m.arms?.agent||{},h=m.arms?.holdout||{};const ci=m.difference_ci95||[];return `<div class="impact-row"><div class="impact-label"><span>${esc(human(name))}</span><b>${m.difference==null?'Unknown':(m.difference*100).toFixed(2)+' pp'}</b></div><div class="impact-bars"><div class="bar-track"><div class="bar" style="width:${Math.max(0,Math.min(100,(a.rate||0)*100))}%"></div></div><div class="bar-track"><div class="bar holdout" style="width:${Math.max(0,Math.min(100,(h.rate||0)*100))}%"></div></div></div><div class="impact-note">${a.rate==null?'Unknown':(a.rate*100).toFixed(1)+'%'} (${n(a.successes)}/${n(a.users)}) vs ${h.rate==null?'Unknown':(h.rate*100).toFixed(1)+'%'} (${n(h.successes)}/${n(h.users)})${ci.length?' · 95% CI '+ci.map(v=>(v*100).toFixed(2)).join(' to ')+' pp':''}</div></div>`;}).join('')}<p class="inline-note">Simulator evidence only. Goal success and exposure are distinct from causal impact. Intervals and missing coverage must accompany the comparison.</p></section><section class="panel report-block"><div class="section-label">RELIABILITY & COST</div><h2>Operational checks</h2>${[['Independent validation',validation.passed===true?'Passed':validation.passed===false?'Failed':'Not available'],['Holdout actions',diagnostic.holdout_action_count],['Over 60 seconds',latency.over_60_seconds],['All decisions p95',(latency.milliseconds?.p95??'—')+' ms'],['Live OpenAI requests',latency.live_openai?.api_requests??0],['Live-only p95',latency.live_openai?.milliseconds?.p95==null?'No live samples':latency.live_openai.milliseconds.p95+' ms'],['Accounted model spend',money(meta.provider_summary?.run_accounted_usd)]].map(([k,v])=>`<div class="key-value"><span>${esc(k)}</span><b>${esc(v??'—')}</b></div>`).join('')}<p class="inline-note">${esc(meta.scheduler||'Replay scheduling is not a production HTTP capacity benchmark.')}</p><details><summary>Complete report data</summary><pre>${pretty(reports)}</pre></details></section></div>`;
}
async function loadJobs(){
  const jobs=await api('/jobs');$('job-list').innerHTML=jobs.map(j=>`<div class="panel job-card"><div><span class="pill ${j.status==='FAILED'?'danger':['RUNNING','QUEUED'].includes(j.status)?'warning':''}">${esc(human(j.status))}</span><h3>${esc(human(j.kind))} · ${esc(j.request.user_id||j.request.trigger_event_id||j.request.provider)}</h3><p>${esc(j.progress?.message||j.error||j.result?.note||'Background job')}<br>${date(j.updated_at)}</p><div class="job-id">${esc(j.job_id)}</div></div>${j.result?.run_id?`<button class="secondary" data-job="${esc(j.job_id)}">Open result ↗</button>`:j.status==='FAILED'?`<button class="secondary" data-retry="${esc(j.job_id)}">Retry job${j.request.provider==='openai'?' · uses model budget':''}</button>`:''}</div>`).join('')||'<div class="panel empty large">No API jobs yet.<small>Try a supplied trigger in Playground or start a replay in Evaluation.</small></div>';
  $('job-list').querySelectorAll('[data-retry]').forEach(el=>el.onclick=async()=>{el.disabled=true;try{await api('/jobs/'+el.dataset.retry+'/retry',{method:'POST',body:{}});await loadJobs();}catch(e){error(e.message);el.disabled=false;}});
  $('job-list').querySelectorAll('[data-job]').forEach(el=>el.onclick=()=>{const j=jobs.find(j=>j.job_id===el.dataset.job);openResult(j.result).catch(e=>error(e.message));});
}
async function startReplay(){
  $('start-replay').disabled=true;error('');
  try{const job=await api('/replays',{method:'POST',body:{provider:$('replay-provider').value,decisions:12}});notice('Replay accepted. Progress and results appear in Background jobs.');setScreen('jobs');await loadJobs();const poll=async()=>{const latest=await api('/jobs/'+job.job_id);if(state.screen==='jobs')await loadJobs();if(['QUEUED','RUNNING'].includes(latest.status))setTimeout(()=>poll().catch(e=>error(e.message)),1500);else{await loadRuns();notice(latest.status==='SUCCEEDED'?'Replay completed. Open its result from Background jobs.':latest.error);}};setTimeout(()=>poll().catch(e=>error(e.message)),1500);}catch(e){error(e.message);}finally{$('start-replay').disabled=false;}
}
function debounce(fn,delay=250){let timer;return(...args)=>{clearTimeout(timer);timer=setTimeout(()=>fn(...args).catch(e=>error(e.message)),delay);};}
document.querySelectorAll('[data-screen]').forEach(el=>el.onclick=()=>setScreen(el.dataset.screen));
$('run-select').onchange=async()=>{state.run=$('run-select').value;state.offset=0;state.selected=null;error('');try{await loadDecisions();if(state.screen==='reports')await loadReports();}catch(e){error(e.message);}};
$('decision-items').onclick=event=>{const el=event.target.closest('[data-decision]');if(el)selectDecision(el.dataset.decision).catch(e=>error(e.message));};
$('decision-detail').onclick=event=>{const el=event.target.closest('[data-tab]');if(el){state.tab=el.dataset.tab;renderTab();}};
for(const id of ['provider-filter','status-filter'])$(id).onchange=()=>{state.offset=0;loadDecisions().catch(e=>error(e.message));};
$('search').oninput=debounce(async()=>{state.offset=0;await loadDecisions();});
$('refresh-decisions').onclick=()=>loadDecisions().catch(e=>error(e.message));
$('previous').onclick=()=>{state.offset=Math.max(0,state.offset-state.limit);loadDecisions().catch(e=>error(e.message));};
$('next').onclick=()=>{state.offset+=state.limit;loadDecisions().catch(e=>error(e.message));};
$('trigger-search').oninput=debounce(loadTriggers);
document.querySelectorAll('[data-mode]').forEach(el=>el.onclick=()=>{state.mode=el.dataset.mode;document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('selected',b===el));$('source-form').hidden=state.mode!=='source';$('live-form').hidden=state.mode!=='live';if(state.mode==='live')loadLiveExample().catch(e=>error(e.message));});
async function loadLiveExample(){
  const generation=(state.exampleGeneration||0)+1;state.exampleGeneration=generation;state.exampleLoading=true;
  $('live-properties-help').textContent='Loading the event requirements…';
  try{
    const data=await api('/events/examples?'+new URLSearchParams({event_type:$('live-kind').value,dialect:$('live-dialect').value}));
    if(generation!==state.exampleGeneration)return;
    $('live-properties').value=JSON.stringify(data.properties,null,2);
    $('live-properties-help').textContent=(data.required_properties.length?'Required properties: '+data.required_properties.join(', ')+'. ':'')+(data.example_title?'Example title: '+data.example_title+'. ':'')+data.hint;
  }catch(e){if(generation===state.exampleGeneration){$('live-properties').value='{}';$('live-properties-help').textContent=e.message;throw e;}}
  finally{if(generation===state.exampleGeneration)state.exampleLoading=false;}
}
$('live-kind').onchange=()=>loadLiveExample().catch(e=>error(e.message));
$('live-dialect').onchange=()=>loadLiveExample().catch(e=>error(e.message));
$('reset-live-example').onclick=()=>loadLiveExample().catch(e=>error(e.message));
$('demo-provider').onchange=()=>{const live=$('demo-provider').value==='openai';$('run-decision').innerHTML=(live?'Run with OpenAI':'Run offline decision')+' <span>↗</span>';$('provider-help').textContent=live?'This explicitly starts paid inference, subject to the existing per-job and cumulative local spending caps.':'Same verification and publication checks, with the deterministic offline planner.';};
$('run-decision').onclick=runDecision;$('start-replay').onclick=startReplay;$('refresh-reports').onclick=()=>loadReports().catch(e=>error(e.message));$('refresh-jobs').onclick=()=>loadJobs().catch(e=>error(e.message));
(async()=>{try{state.config=await api('/config');await loadRuns();const preferred=state.runs.find(r=>r.run_id==='runs/my_openai_demo')||state.runs.find(r=>r.metadata.provider==='openai');if(preferred){state.run=preferred.run_id;$('run-select').value=state.run;$('provider-filter').value='openai';}if(!state.config.openai_available){document.querySelectorAll('option[value="openai"]').forEach(el=>{el.disabled=true;el.textContent+=' · key not configured';});}await Promise.all([loadDecisions(),loadTriggers()]);}catch(e){error(e.message);document.querySelector('.online-dot').textContent='Backend unavailable';}})();
