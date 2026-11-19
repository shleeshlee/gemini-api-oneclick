// Run actual panel rendering with synthetic states; no browser or accounts needed.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
for (const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) {
  if (match[1].trim()) new vm.Script(match[1]);
}
const render = html.slice(html.indexOf('function renderGrid()'), html.indexOf('function fmtReset('));
function card(extra) {
  const elements = {};
  const context = {
    accounts: [{num: 9, has_cookie: true}],
    gatewayStatus: {containers: [{num: 9, enabled: true, healthy: false, ...extra}], available: 0},
    names: {}, localRestarting: new Set(), currentMode: 'worker',
    esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    groupColor: () => '', tierChip: () => '', renderUsage: () => '', setTimeout: () => {},
    document: {getElementById(id) {return elements[id] ||= {dataset: {painted: '1'}, style: {}};}},
  };
  vm.runInNewContext(render + '\nrenderGrid();', context);
  return elements.grid.innerHTML;
}
let output = card({needs_cookie: true, last_error_type: 'auth_failed', last_error: 'HTTP 401 Unauthorized: 需要重新认证'});
assert.match(output, /认证失败/);
assert.match(output, /HTTP 401 Unauthorized/);
assert.match(output, /恢复此账号/);
assert.doesNotMatch(output, /正在验证登录/);
output = card({last_error_type: 'init_timeout', last_error: '初始化超时（总时限 60 秒）'});
assert.match(output, /card-status yellow/);
assert.match(output, /初始化超时/);
assert.doesNotMatch(output, /正在验证登录/);
output = card({initializing: true, needs_cookie: true, last_error: 'old failure'});
assert.match(output, /正在验证登录/);
assert.doesNotMatch(output, /old failure/);
output = card({needs_cookie: true, last_error: '<script>synthetic</script>'});
assert.match(output, /&lt;script&gt;/);
assert.doesNotMatch(output, /<script>/);
console.log('Panel syntax and 4 login-state rendering cases passed');
