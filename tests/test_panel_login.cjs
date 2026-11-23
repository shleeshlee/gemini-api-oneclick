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
const subscription = html.slice(html.indexOf('function subscriptionTier('), html.indexOf('\nrenderThemeDots();'));
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
  vm.runInNewContext(subscription + render + '\nrenderGrid();', context);
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
for (const [tierId, label] of [[3, 'ULTRA 20x'], [6, 'ULTRA 5x']]) {
  output = card({healthy: true, tier: 'free', usage: {tier_id: tierId}, group: 'PRO'});
  assert.match(output, new RegExp(label));
  assert.match(output, /tier-ultra/);
  assert.doesNotMatch(output, />FREE</);
}
output = card({healthy: true, tier: 'ultra', usage: {tier_id: 1}});
assert.match(output, />FREE</);
assert.doesNotMatch(output, /tier-ultra/);
for (const usage of [null, {tier_id: 99}, {tier_id: '6'}]) {
  output = card({healthy: true, tier: 'free', usage});
  assert.match(output, /待识别/);
  assert.doesNotMatch(output, />FREE</);
}
output = card({needs_cookie: true, usage: {tier_id: 6}, usage_error: 'unavailable'});
assert.match(output, /ULTRA 5x/);
assert.match(output, /最近一次用量查询失败/);
assert.match(output, /认证失败/);
console.log('Subscription labels, card colors, unknown plans and independent login states passed');
