"""policy.js state logic in plain node (no browser, no jsdom): a poll that is in flight while the
owner edits must not discard the edit, an open cell editor blocks reloads, and a save sends the
loaded revision as If-Match (regressions from the Codex upgrade review). Skipped without node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const els = new Map(), grids = new Map();
const $ = (id) => { if (!els.has(id)) els.set(id, {id, value: id === 'chart-days' ? '14' : '', hidden: false,
  disabled: false, textContent: '', handlers: {}, addEventListener(k, f) { this.handlers[k] = f; }}); return els.get(id); };
let policy = {per_order_cap: '50.00', daily_cap: '150.00', hard_max_per_order: '500.00',
  allowed_merchants: ['a', 'b'], blocked_merchants: [], allowed_categories: ['x'], revision: 'r1'};
let delay = false, release = null, editingCells = [], puts = [];
const polls = [];
const api = async (path, opts) => {
  if (opts && opts.method === 'PUT') { puts.push(opts); return {result: 'UPDATED', change: {changes: {per_order_cap: {}}}}; }
  if (path === '/policy') { if (delay) await new Promise((r) => (release = r)); return structuredClone(policy); }
  if (path === '/catalog') return [];
  if (path === '/policy/history') return [];
  return {days: [], daily_cap: '150.00'};
};
const ctx = {$, api, document: {hidden: false, head: {appendChild() {}}}, sessionStorage: {getItem() { return null; }, setItem() {}},
  agGrid: {createGrid(el, opt) { grids.set(el.id, opt); return {refreshCells() {}, setGridOption() {}, getEditingCells: () => editingCells}; }},
  toast() {}, usd: (v) => '$' + v, tokenInput: {value: 'tok'}, refresh() {}, setInterval: (f) => polls.push(f),
  window: {agCharts: {}}, agCharts: {AgCharts: {create: () => ({update() {}})}}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  const out = {};
  $('tab-policy').handlers.click(); await tick(); await tick();
  const col = grids.get('policy-caps').columnDefs.find((c) => c.colId === 'value'), row = {key: 'per_order_cap'};
  // 1) poll in flight, owner edits, poll returns -> edit kept
  delay = true; policy.revision = 'r2'; polls[0](); await tick();
  col.valueSetter({data: row, newValue: '75'});
  release(); delay = false; await tick(); await tick();
  out.afterPoll = {value: col.valueGetter({data: row}), dirty: $('policy-dirty').textContent};
  // 2) save sends the loaded revision (r1) as If-Match
  await $('policy-save').handlers.click(); await tick();
  out.ifMatch = puts[0].headers['If-Match'];
  // 3) open editor blocks a reload of a new revision
  policy.revision = 'r3'; policy.per_order_cap = '60.00'; editingCells = [{}];
  polls[0](); await tick(); await tick();
  out.whileEditing = col.valueGetter({data: row});
  editingCells = []; polls[0](); await tick(); await tick();
  out.afterEditing = col.valueGetter({data: row});
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_policy_js_keeps_edits_and_sends_revision(tmp_path):
    h = tmp_path / "harness.cjs"
    h.write_text(HARNESS)
    r = subprocess.run([NODE, str(h), str(ROOT / "app/static/policy.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["afterPoll"] == {"value": "75.00", "dirty": "1 unsaved change"}
    assert out["ifMatch"] == "r1"
    assert out["whileEditing"] != "60.00"
    assert out["afterEditing"] == "60.00"
