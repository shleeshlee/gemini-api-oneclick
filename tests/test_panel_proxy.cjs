// Exercise actual panel handlers with synthetic settings and delayed HTTP responses.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const section = (from, to) => html.slice(html.indexOf(from), html.indexOf(to, html.indexOf(from)));
const code = [
  section('async function saveName(', '// ── Login'),
  section('function safeProxyAddress(', '// ── Usage limits'),
  section('// ── Proxy settings and account assignment', 'function showErrorHistory('),
  section('async function saveGroup(', "document.addEventListener('keydown'"),
  section('async function doDeploy(', '// ── Toast'),
].join('\n');

function element() {
  return {value:'', textContent:'', innerHTML:'', disabled:false, children:[], dataset:{}, style:{},
    classList:{add(){}, remove(){}}, focus(){},
    appendChild(child){this.children.push(child);},
    replaceChildren(){this.children=[]; this.innerHTML='';},
  };
}
function deferred() {
  let resolve;
  const promise = new Promise(done => {resolve = done;});
  return {promise, resolve};
}
function response(data = {}, ok = true) {return {ok, json:async () => data};}
function setup(handler = async () => response()) {
  const elements = {};
  const calls = [], notices = [], restarting = [];
  const context = {
    URL, currentNum:20, modalVersion:1, names:{}, gatewayStatus:{containers:[{num:20, name:'Old', group:''}]},
    proxyProfiles:[{id:'a', name:'A', url:'http://127.0.0.1:8001'}, {id:'b', name:'B', url:'socks5://127.0.0.1:8002'}],
    proxyAssignments:{'20':'a'}, proxySettingsLoaded:true, proxySettingsError:'', proxyLoadVersion:0, proxyMutationVersion:0,
    pendingProxySaves:new Map(), currentMode:'worker',
    localRestarting:{add:num => restarting.push(num), delete(){}, has(){return false;}},
    localStorage:{setItem(){}}, authHeaders:() => ({}), setTimeout(){}, renderGrid(){}, loadAll:async () => {}, populateGroupSelect(){},
    toast:(...args) => notices.push(args),
    document:{getElementById:id => elements[id] ||= element(), createElement:element},
    fetch:async (url, options = {}) => {calls.push({url, options}); return handler(url, options);},
  };
  vm.createContext(context);
  vm.runInContext(code, context);
  for (const id of ['mPsid','mPsidts','mName','mGroup','mProxy','mProxyMsg','modalMsg','deployBtn']) context.document.getElementById(id);
  elements.mName.value = 'New name';
  return {context, elements, calls, notices, restarting};
}

(async () => {
  // An empty Cookie form is a metadata-only save, never a deployment/recovery.
  let test = setup();
  await test.context.doDeploy();
  assert.deepEqual(test.calls.map(x => x.url).sort(), ['/gateway/group/20','/gateway/name/20']);
  assert.equal(test.restarting.length, 0);
  assert.match(test.elements.modalMsg.textContent, /信息已保存/);
  assert.equal(test.context.names[20], 'New name');

  // Either incomplete Cookie field is rejected before any mutation.
  for (const id of ['mPsid','mPsidts']) {
    test = setup(); test.elements[id].value = 'synthetic';
    await test.context.doDeploy();
    assert.equal(test.calls.length, 0);
    assert.match(test.elements.modalMsg.textContent, /两项 Cookie/);
  }

  // Metadata failure is visible and cannot accidentally trigger cookie deployment.
  test = setup(async url => response({detail:'group save denied'}, !url.includes('/group/')));
  await test.context.doDeploy();
  assert.match(test.elements.modalMsg.textContent, /group save denied/);
  assert.equal(test.elements.modalMsg.className, 'modal-msg err');
  assert.equal(test.calls.some(x => x.url.includes('/deploy/')), false);

  // Complete Cookie input still uses the established deployment endpoint.
  test = setup(); test.elements.mPsid.value='synthetic-psid'; test.elements.mPsidts.value='synthetic-ts';
  await test.context.doDeploy();
  const deployment = test.calls.find(x => x.url === '/gateway/deploy/20');
  assert.deepEqual(JSON.parse(deployment.options.body), {psid:'synthetic-psid', psidts:'synthetic-ts'});
  assert.deepEqual(test.restarting, [20]);

  // Selecting a proxy saves immediately; no deployment or Cookie fields are involved.
  test = setup(); test.elements.mProxy.value = 'b';
  await test.context.saveSlotProxy();
  assert.deepEqual(test.calls.map(x => x.url), ['/gateway/proxy/20']);
  assert.deepEqual(JSON.parse(test.calls[0].options.body), {proxy_id:'b'});
  assert.equal(test.context.proxyAssignments['20'], 'b');
  assert.equal(test.elements.mProxy.value, 'b');
  assert.equal(test.elements.mProxy.disabled, false);
  assert.equal(test.context.pendingProxySaves.size, 0);

  // A failed assignment rolls the selection back to the saved choice.
  test = setup(async () => response({detail:'account busy'}, false)); test.elements.mProxy.value='b';
  assert.equal(await test.context.saveSlotProxy(), false);
  assert.equal(test.context.proxyAssignments['20'], 'a');
  assert.equal(test.elements.mProxy.value, 'a');
  assert.match(test.elements.mProxyMsg.textContent, /account busy/);

  // Metadata save waits for an already-started proxy save and reports its failure.
  const pendingAssignment = deferred();
  test = setup(async url => url.includes('/proxy/') ? pendingAssignment.promise : response());
  test.elements.mProxy.value='b';
  const assignment = test.context.saveSlotProxy();
  const metadata = test.context.doDeploy();
  pendingAssignment.resolve(response({detail:'proxy rejected'}, false));
  await Promise.all([assignment, metadata]);
  assert.match(test.elements.modalMsg.textContent, /代理未保存/);
  assert.equal(test.calls.some(x => x.url.includes('/deploy/')), false);

  // Failure to load the pool must not silently treat a saved binding as default.
  test = setup(async () => {throw new Error('offline');});
  await test.context.loadProxySettings();
  assert.equal(test.context.proxyAssignments['20'], 'a');
  assert.equal(test.elements.mProxy.disabled, true);
  assert.match(test.elements.mProxy.innerHTML, /加载失败/);
  assert.match(test.context.proxyLabel(20), /A/);

  // A delayed GET that predates a successful assignment cannot overwrite it.
  const oldRead = deferred();
  test = setup(async url => url === '/gateway/proxies' ? oldRead.promise : response());
  const loading = test.context.loadProxySettings();
  test.elements.mProxy.value='b'; await test.context.saveSlotProxy();
  oldRead.resolve(response({proxies:test.context.proxyProfiles, assignments:{'20':'a'}}));
  await loading;
  assert.equal(test.context.proxyAssignments['20'], 'b');

  // Changing cards while a save is pending must not modify the new card's controls.
  const delayed = deferred();
  test = setup(async () => delayed.promise); test.elements.mProxy.value='b';
  const saving = test.context.saveSlotProxy();
  test.context.currentNum=7; test.context.modalVersion=2;
  test.elements.mProxy.value='another-card'; test.elements.mProxyMsg.textContent='untouched';
  delayed.resolve(response()); await saving;
  assert.equal(test.context.proxyAssignments['20'], 'b');
  assert.equal(test.elements.mProxy.value, 'another-card');
  assert.equal(test.elements.mProxyMsg.textContent, 'untouched');

  // Deployment captures the original account and never closes/changes a later modal.
  const nameSave = deferred();
  test = setup(async url => url.includes('/name/') ? nameSave.promise : response());
  test.elements.mPsid.value='synthetic-psid'; test.elements.mPsidts.value='synthetic-ts';
  const deploying = test.context.doDeploy();
  test.context.currentNum=7; test.context.modalVersion=2; test.elements.modalMsg.textContent='other modal';
  nameSave.resolve(response()); await deploying;
  assert.equal(test.calls.some(x => x.url === '/gateway/deploy/20'), true);
  assert.equal(test.calls.some(x => x.url === '/gateway/deploy/7'), false);
  assert.equal(test.elements.modalMsg.textContent, 'other modal');

  // Card rendering shows the assigned address but never URL authentication fields.
  test = setup();
  Object.assign(test.context, {accounts:[{num:20,has_cookie:true}],
    esc:value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    groupColor:() => '', tierChip:() => '', renderUsage:() => ''});
  test.context.proxyProfiles=[{id:'a',name:'http://synthetic-user:synthetic-secret@proxy.test:8080',url:'http://synthetic-user:synthetic-secret@proxy.test:8080'}];
  test.context.gatewayStatus.containers[0].enabled=true;
  test.elements.grid=element(); test.elements.grid.dataset.painted='1';
  vm.runInContext(section('function renderGrid()', 'function fmtReset('), test.context);
  test.context.renderGrid();
  assert.match(test.elements.grid.innerHTML, /proxy\.test:8080/);
  assert.doesNotMatch(test.elements.grid.innerHTML, /synthetic-user|synthetic-secret/);
  console.log('Panel proxy, metadata-only save and async isolation cases passed');
})().catch(error => {console.error(error); process.exitCode=1;});
