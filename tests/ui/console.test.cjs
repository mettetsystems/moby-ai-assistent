// Run with node --test tests/ui/console.test.cjs. No model server required.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const script = readFileSync('src/mobyai/console/ui.html', 'utf8').split('<script>')[1].split('</script>')[0];
function page() {
  const elements = new Map();
  const calls = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, {value: id === 'project' ? 'demo' : '', checked:false,
      options:[], focus(){}, classList:{toggle(){}, add(){}, remove(){}, contains(){return false;}}, innerHTML:'', textContent:''});
    return elements.get(id);
  };
  const context = vm.createContext({document:{getElementById:element, querySelectorAll:()=>[]}, TextEncoder, TextDecoder,
    setInterval(){}, clearInterval(){}, confirm:()=>false, prompt:()=>null,
    fetch:async (url, options) => {
      calls.push([url, options]);
      const body = url === '/api/workloads/prepare-stop' ? {token:'proof', project_id:'demo'} :
        url === '/api/projects' ? {projects:[{project_id:'demo', name:'Demo', state:'WORKING'}]} :
        url.startsWith('/api/monitor') ? {project:{name:'Demo',state:'WORKING'},steps:[],failed:[],workload:{can_stop:true}} :
        {models:{}};
      return {ok:true, status:200, json:async()=>body, text:async()=>JSON.stringify(body)};
    }});
  vm.runInContext(script, context);
  return {context,element,calls};
}
test('first confirmation cancel makes no stop request', async()=>{
  const p=page();
  await p.element('stop-workload').onclick();
  assert.equal(p.calls.filter(([url])=>url.includes('/workloads/')).length,0);
});
test('incorrect second confirmation cannot stop', async()=>{
  const p=page();p.context.confirm=()=>true;p.context.prompt=()=>'wrong';
  await p.element('stop-workload').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/prepare-stop').length,1);
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/stop').length,0);
});
test('both confirmations send proof and exact project ID', async()=>{
  const p=page();p.context.confirm=()=>true;p.context.prompt=()=>'demo';
  await p.element('stop-workload').onclick();
  const stop=p.calls.find(([url])=>url==='/api/workloads/stop');
  assert.deepEqual(JSON.parse(stop[1].body),{project_id:'demo',token:'proof',confirmation:'demo'});
});
test('HTML response explains stale server without raw JSON error', async()=>{
  const p=page();p.context.fetch=async()=>({status:501,text:async()=>'<html>Unsupported POST</html>'});
  await assert.rejects(vm.runInContext('apiJson("/api/projects")',p.context),/HTTP 501.*Restart the console/);
});

test('traces are lazy-loaded and not fetched on other tabs', async()=>{
  const p=page();
  assert.equal(p.calls.filter(([url])=>url.startsWith('/api/traces')).length,0);
  vm.runInContext('showTab("settings")',p.context);
  assert.equal(p.calls.filter(([url])=>url.startsWith('/api/traces')).length,0);
  await vm.runInContext('loadTraces()',p.context);
  assert.equal(p.calls.filter(([url])=>url.startsWith('/api/traces')).length,1);
  vm.runInContext('showTab("traces")',p.context);
  assert.equal(p.calls.filter(([url])=>url.startsWith('/api/traces')).length,1);
});
test('trace labels are escaped as text', async()=>{
  const p=page();
  p.context.fetch=async()=>({ok:true,text:async()=>JSON.stringify({experiment:'MobyAI',enabled:true,
    traces:[{trace_id:'tr-1',name:'<img src=x onerror=alert(1)>',status:'ERROR',started_ms:1000,duration_ms:3}]})});
  await vm.runInContext('loadTraces()',p.context);
  assert.ok(!p.element('trace-list').innerHTML.includes('<img'));
  assert.ok(p.element('trace-list').innerHTML.includes('&lt;img'));
});
test('resume sends only the saved project ID, never the target input', async()=>{
  const p=page();p.element('monitor-target').value='/some/new/location';
  await p.element('resume-workload').onclick();
  const request=p.calls.find(([url])=>url==='/api/workloads/resume');
  assert.deepEqual(JSON.parse(request[1].body),{project_id:'demo'});
});
test('restart cancellation makes no request', async()=>{
  const p=page();
  await p.element('restart-workload').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/restart').length,0);
});
test('restart sends the chosen fresh directory and source confirmation', async()=>{
  const p=page();p.context.confirm=()=>true;p.element('monitor-target').value='/new target';
  await p.element('restart-workload').onclick();
  const request=p.calls.find(([url])=>url==='/api/workloads/restart');
  assert.deepEqual(JSON.parse(request[1].body),{project_id:'demo',target_directory:'/new target',confirmation:'demo'});
});
test('change target requires a destination and confirmation', async()=>{
  const p=page();p.context.confirm=()=>true;
  await p.element('change-target').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/target').length,0);
  p.element('monitor-target').value='/new target';p.context.confirm=()=>false;
  await p.element('change-target').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/target').length,0);
  p.context.confirm=()=>true;
  await p.element('change-target').onclick();
  const request=p.calls.find(([url])=>url==='/api/workloads/target');
  assert.deepEqual(JSON.parse(request[1].body),{project_id:'demo',target_directory:'/new target'});
});

test('runtime display totals hours beyond a day and distinguishes elapsed pauses', async()=>{
  const p=page();
  const original=p.context.fetch;
  p.context.fetch=async(url,options)=>url.startsWith('/api/monitor') ? {
    ok:true, json:async()=>({project:{name:'Demo',state:'PAUSED'},steps:[],failed:[],
      runtime:{active_seconds:90061,elapsed_seconds:100000,running:false,estimated:true}})
  } : original(url,options);
  await vm.runInContext('refreshMonitor()',p.context);
  assert.match(p.element('runtime-tracker').textContent,/Total runtime: 25h 01m 01s/);
  assert.match(p.element('runtime-tracker').textContent,/Elapsed including pauses: 27h 46m 40s/);
  assert.match(p.element('runtime-tracker').textContent,/Stopped · Estimated/);
});

test('delete uses inline confirmation and requires an exact project ID',async()=>{
  const p=page();
  p.context.confirm=p.context.prompt=()=>{throw new Error('Native dialogs must not be used');};
  p.element('delete-project').onclick();
  p.element('delete-project-input').value='wrong';
  await p.element('confirm-delete-project').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/delete').length,0);
  p.element('delete-project-input').value='demo';
  await p.element('confirm-delete-project').onclick();
  const call=p.calls.find(([url])=>url==='/api/workloads/delete');
  assert.deepEqual(JSON.parse(call[1].body),{project_id:'demo',confirmation:'demo'});
});
test('delete cancellation preserves project and polling waits during confirmation',async()=>{
  const p=page();
  p.element('delete-project').onclick();
  const count=p.calls.length;
  await vm.runInContext('loadMonitor()',p.context);
  assert.equal(p.calls.length,count);
  await p.element('cancel-delete-project').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/delete').length,0);
  assert.equal(p.element('project').disabled,false);
});
test('delete failure stays inline and releases controls for retry or cancellation',async()=>{
  const p=page();
  p.element('delete-project').onclick();
  p.element('delete-project-input').value='demo';
  p.context.fetch=async()=>{throw new Error('Connection lost');};
  await p.element('confirm-delete-project').onclick();
  assert.match(p.element('delete-status').textContent,/Connection lost/);
  assert.equal(p.element('cancel-delete-project').disabled,false);
  assert.equal(p.element('confirm-delete-project').disabled,false);
});
test('monitor response started before deletion cannot overwrite confirmation state',async()=>{
  const p=page();
  const original=p.context.fetch;
  let release;
  let reached;
  const started=new Promise(resolve=>{reached=resolve;});
  p.context.fetch=async(url,options)=>{
    if (!url.startsWith('/api/monitor')) return original(url,options);
    reached();
    return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({project:{name:'Stale',state:'PAUSED'},workload:{can_delete:true},steps:[]})});});
  };
  const refresh=vm.runInContext('loadMonitor()',p.context);
  await started;
  p.element('delete-project').onclick();
  release();
  await refresh;
  assert.equal(p.element('delete-project').disabled,true);
  assert.equal(p.element('delete-project-label').textContent,'demo');
  assert.ok(!p.element('monitor-status').textContent.includes('Stale'));
});
test('monitor colors current status even when a passed task has failure history',async()=>{
  const p=page();
  const original=p.context.fetch;
  p.context.fetch=async(url,options)=>url.startsWith('/api/monitor') ? {
    ok:true,json:async()=>({project:{name:'Demo',state:'PAUSED'},steps:[
      {task_id:'PASS',name:'Recovered',status:'passed',failed:true,attempt_count:2},
      {task_id:'FAIL',name:'Failed',status:'failed',failed:true,attempt_count:1},
      {task_id:'TEST',name:'Testing',status:'testing',failed:false,attempt_count:1}
    ],failed:[]})
  } : original(url,options);
  await vm.runInContext('refreshMonitor()',p.context);
  assert.equal(p.element('project-state').className,'badge state-paused');
  assert.match(p.element('chips').innerHTML,/chip state-passed.*PASS passed/);
  assert.match(p.element('steps').innerHTML,/data-task="PASS" class="step state-passed"/);
  assert.match(p.element('steps').innerHTML,/badge state-testing/);
  assert.match(p.element('steps').innerHTML,/badge state-failed/);
});
test('existing code review requires an explicit checked approval for the displayed revision',async()=>{
  const p=page();
  const data={review:{proposal:{revision:'review-v1',approved:false,plan:{tasks:[]}}}};
  p.context.reviewData=data;
  vm.runInContext('renderReview(reviewData,"demo")',p.context);
  await p.element('approve-plan').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/approve-plan').length,0);
  p.element('plan-reviewed').checked=true;
  await p.element('approve-plan').onclick();
  const call=p.calls.find(([url])=>url==='/api/workloads/approve-plan');
  assert.deepEqual(JSON.parse(call[1].body),{project_id:'demo',revision:'review-v1'});
});
test('changing the proposed revision clears approval and report content is plain text',()=>{
  const p=page();
  p.context.reviewData={review:{proposal:{revision:'v1',plan:{tasks:[]}},logic_report:{summary:'<script>bad()</script>'}}};
  vm.runInContext('renderReview(reviewData,"demo")',p.context);
  p.element('plan-reviewed').checked=true;
  p.context.reviewData.review.proposal.revision='v2';
  vm.runInContext('renderReview(reviewData,"demo")',p.context);
  assert.equal(p.element('plan-reviewed').checked,false);
  assert.equal(p.element('review-report').innerHTML,'');
  assert.match(p.element('review-report').textContent,/<script>/);
});


test('secret submission clears input even on failure and sends CSRF proof', async()=>{
  const p=page();
  p.context.fetch=async(url,options)=> {
    if (options?.method === 'POST') {
      assert.equal(p.element('secret-value').value,'');
      assert.equal(options.headers['X-MobyAI-Vault-CSRF'],'proof');
      assert.equal(JSON.parse(options.body).value,'dummy-token');
      return {ok:false,status:400,text:async()=>JSON.stringify({error:'Vault locked'})};
    }
    return {ok:true,text:async()=>JSON.stringify({available:true,backend:'Local vault',message:'Ready',csrf_token:'proof',providers:[{id:'github',name:'GitHub',configured:false,usage:'Storage only'}]})};
  };
  await vm.runInContext('loadSecrets()',p.context);
  p.element('secret-value').value='dummy-token';
  await p.element('secrets-form').onsubmit({preventDefault(){}});
  assert.equal(p.element('secret-value').value,'');
  assert.equal(p.element('secret-result').textContent,'Vault locked');
});
test('secret removal requires explicit confirmation', async()=>{
  const p=page();
  vm.runInContext('secretsState = {available:true,providers:[],csrf_token:"proof"}',p.context);
  await p.element('secret-remove').onclick();
  assert.equal(p.calls.filter(([url])=>url==='/api/secrets').length,0);
});

for (const [state,running,error,failed,kind,label] of [
  ['VALIDATING',true,'Schema repair failed',[], 'recovery','Recovery in progress:'],
  ['ESCALATING',true,'Test failure',[], 'recovery','Recovery in progress:'],
  ['PAUSED',false,'Context exhausted',[], 'stopped','Project stopped'],
  ['FAILED',false,'Unrecoverable error',[], 'stopped','Project stopped'],
  ['READY',false,'No eligible tasks',[], 'stopped','Project stopped'],
  ['WORKING',true,null,['OLD'], 'recovery','Failure history:'],
  ['COMPLETED',false,null,['OLD'], 'recovery','Failure history:'],
  ['WORKING',true,null,[], 'hidden',''],
]) {
  test(`monitor error severity ${state} ${error || failed.length}`,async()=>{
    const p=page(); const original=p.context.fetch;
    p.context.fetch=async(url,options)=>url.startsWith('/api/monitor') ? {
      ok:true,json:async()=>({project:{name:'Demo',state,last_error:error},steps:[],failed,workload:{running}})
    } : original(url,options);
    await vm.runInContext('refreshMonitor()',p.context);
    assert.equal(p.element('fail-banner').className,'banner '+kind);
    assert.ok(p.element('fail-banner').textContent.startsWith(label));
  });
}

test('timeout approval requires exact confirmation and does not resume',async()=>{
  const p=page();
  p.context.timeoutData={project:{state:'PAUSED'},workload:{running:false},timeout_policy:{ceiling_seconds:1800,approval_required:true}};
  vm.runInContext('renderTimeoutApproval(timeoutData,"demo")',p.context);
  await p.element('approve-timeout').onclick();
  assert.equal(p.calls.filter(([url])=>url.includes('approve-timeout')).length,0);
  p.element('timeout-confirmation').value='demo';
  p.element('timeout-seconds').value='2400';
  await p.element('approve-timeout').onclick();
  const call=p.calls.find(([url])=>url.includes('approve-timeout'));
  assert.deepEqual(JSON.parse(call[1].body),{project_id:'demo',seconds:2400,confirmation:'demo'});
  assert.equal(p.calls.filter(([url])=>url==='/api/workloads/resume').length,0);
});

test('timeout confirmation resets on project change and active runs cannot approve',()=>{
  const p=page();
  p.context.timeoutData={project:{state:'PAUSED'},workload:{running:false},timeout_policy:{ceiling_seconds:1800}};
  vm.runInContext('renderTimeoutApproval(timeoutData,"demo")',p.context);
  p.element('timeout-confirmation').value='demo';
  p.element('project').value='other';
  vm.runInContext('renderTimeoutApproval(timeoutData,"other")',p.context);
  assert.equal(p.element('timeout-confirmation').value,'');
  p.element('timeout-confirmation').value='other';
  p.context.timeoutData.workload.running=true;
  vm.runInContext('renderTimeoutApproval(timeoutData,"other")',p.context);
  assert.equal(p.element('approve-timeout').disabled,true);
});

test('initial plan editor is disabled after approval and polling preserves drafts', async()=>{
  const p=page();
  p.context.reviewData={review:{proposal:{approved:true,revision:'one',plan:{tasks:[]}}},workload:{}};
  vm.runInContext('renderReview(reviewData,"demo")',p.context);
  assert.equal(p.element('edit-plan').disabled,true);
  p.context.reviewData.review.proposal.approved=false;
  vm.runInContext('renderReview(reviewData,"demo")',p.context);
  assert.equal(p.element('edit-plan').disabled,false);
  p.element('edit-plan-json').value='my unsaved draft';
  vm.runInContext('renderReview(reviewData,"demo")',p.context);
  assert.equal(p.element('edit-plan-json').value,'my unsaved draft');
});
test('saving initial plan sends draft version without starting a workload', async()=>{
  const p=page();
  p.context.fetch=async(url,options)=>{
    p.calls.push([url,options]);
    const request=options?.body ? JSON.parse(options.body) : {};
    const body=url==='/api/workloads/edit-plan' ? (request.action==='load' ? {version:'draft-version',plan:{tasks:[]},task_statuses:{}} : {message:'Saved'}) :
      url==='/api/projects' ? {projects:[]} : {project:{name:'Demo',state:'PAUSED'},steps:[],failed:[],workload:{}};
    return {ok:true,text:async()=>JSON.stringify(body)};
  };
  await p.element('edit-plan').onclick();
  p.element('edit-plan-reason').value='Clarify acceptance tests';
  await p.element('save-edited-plan').onclick();
  const requests=p.calls.filter(([url])=>url==='/api/workloads/edit-plan').map(([,options])=>JSON.parse(options.body));
  assert.equal(requests[1].version,'draft-version');
  assert.equal(requests[1].project_id,'demo');
  assert.equal(requests[1].reason,'Clarify acceptance tests');
  assert.equal(p.calls.some(([url])=>/approve-plan|restart|resume/.test(url)),false);
});

test('final pre-build is explicit and reports preparation status', async()=>{
  const p=page();
  assert.equal(p.calls.some(([url])=>url==='/api/final-build'),false);
  p.context.fetch=async(url,options)=>{
    p.calls.push([url,options]);
    return {ok:true,text:async()=>JSON.stringify({state:'building',message:'Downloading'})};
  };
  await p.element('final-build').onclick();
  assert.deepEqual(JSON.parse(p.calls.at(-1)[1].body),{action:'build'});
  assert.equal(p.element('final-build').disabled,true);
  assert.match(p.element('final-build-status').textContent,/Downloading/);
});
test('final review reports render as text, not markup',()=>{
  const p=page();
  p.context.data={review:{final_review:{phase:'final_review',status:'planned',rationale:'<script>bad</script>'}}};
  vm.runInContext('renderReview(data,"demo")',p.context);
  assert.match(p.element('final-pass-report').textContent,/<script>/);
  assert.equal(p.element('final-pass-report').innerHTML,'');
});

test('system prompt editor escapes text and labels activation boundary', () => {
  const p=page();
  vm.runInContext('showSystemPrompts({system_prompts:{worker:"</textarea><script>bad</script>"},default_system_prompts:{worker:"Default"}})',p.context);
  assert.match(p.element('system-prompts').innerHTML,/&lt;\/textarea&gt;/);
  assert.match(p.element('system-prompts').innerHTML,/Worker system prompt/);
  assert.match(p.element('system-prompts').innerHTML,/Restore default/);
});

test('saving includes system prompts and uses null to restore defaults', async () => {
  const p=page();
  await vm.runInContext('loadSettings()',p.context);
  vm.runInContext('defaultSystemPrompts = {worker:"Default worker"}',p.context);
  for(const key of ['planner','replan','orchestrator','worker','final_fail','final_review']) {
    p.element('prompt-'+key).value=key==='worker'?'Default worker':'Custom '+key;
  }
  await p.element('save-settings').onclick();
  const call=p.calls.find(([url,opts])=>url==='/api/settings'&&opts?.method==='PUT');
  const payload=JSON.parse(call[1].body);
  assert.equal(payload.system_prompts.worker,null);
  assert.equal(payload.system_prompts.replan,'Custom replan');
  assert.match(p.element('settings-msg').textContent,/next step or retry attempt/);
});
