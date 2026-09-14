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
