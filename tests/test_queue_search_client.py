"""Exercise queue polling with delayed responses, without touching the library."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("next_term,next_status", [("new", ""), ("", ""), ("old", "failed"), ("new", "failed")])
def test_changed_search_discards_old_response_and_fetches_latest(outcome, next_term, next_status):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the queue browser regression check")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('        async function loadJobs(');
const end = source.indexOf('        function secondsSince(', start);
assert(start >= 0 && end > start);
let queueLoadInFlight = false, queueSearchTerm = 'old', queueStatusFilter = '', allQueueJobs = [];
let queueTotals = null, queueLastUpdatedAt = 0, queuePage = 1;
const QUEUE_SEARCH_LIMIT = 250, QUEUE_PAGE_SIZE = 10;
const queueTab = {innerHTML: ''};
const pending = [], rendered = [];
const apiFetch = url => new Promise((resolve, reject) => pending.push({url, resolve, reject}));
const renderQueueSummary = () => {};
const renderQueuePage = () => rendered.push({term: queueSearchTerm, status: queueStatusFilter, jobs: allQueueJobs});
eval(source.slice(start, end));
(async () => {
    const first = loadJobs();
    queueSearchTerm = process.argv[2];
    queueStatusFilter = process.argv[3];
    await loadJobs(false);
    assert.equal(pending.length, 1, 'requests should stay serial');
    if (process.argv[1] === 'failure') pending[0].reject(new Error('old request failed'));
    else pending[0].resolve({ok: true, json: async () => ({jobs: [{id: 'old'}]})});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(rendered, [], 'outdated results must never render');
    assert(!queueTab.innerHTML.includes('Failed to load queue'));
    assert.equal(pending.length, 2, 'latest search must run as soon as the old request finishes');
    assert.equal(pending[1].url, '/api/jobs?limit=250' + (queueSearchTerm ? `&q=${queueSearchTerm}` : '')
                 + (queueStatusFilter ? '&status=failed' : ''));
    pending[1].resolve({ok: true, json: async () => ({jobs: [{id: 'latest'}], queue_totals: {completed: 1}})});
    await first;
    assert.deepEqual(rendered, [{term: queueSearchTerm, status: queueStatusFilter, jobs: [{id: 'latest'}]}]);
    assert.equal(queueLoadInFlight, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", script, outcome, next_term, next_status],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("stats_count,yours,term,expect,unexpected", [
    (3, 3, "", "<strong>3</strong> failed downloads across your whole queue.", "every account"),
    (5, 3, "", "Stats counts every account's failures; this list only holds yours.", None),
    (3, 3, "boards", "failed downloads matching &ldquo;boards&rdquo;", "every account"),
])
def test_stats_failed_click_lands_on_a_failed_filter(stats_count, yours, term, expect, unexpected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the queue browser regression check")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('static/app.js', 'utf8');
const start = source.indexOf('        function queueFailedFilterStatus(');
const end = source.indexOf('        function getSortedQueueJobs(', start);
assert(start >= 0 && end > start);
const [statsCount, yours, term, expect, unexpected] = process.argv.slice(1);
let queueStatusFilter = '', queueStatsFailedCount = null, queuePage = 4;
let queueSearchTerm = term;
const QUEUE_SEARCH_LIMIT = 250;
const escapeHtml = s => s;
const clicked = [], loads = [];
const document = {querySelector: sel => ({click: () => clicked.push(sel)})};
const loadJobs = () => loads.push({status: queueStatusFilter, term: queueSearchTerm});
eval(source.slice(start, end));
showQueueFailures(Number(statsCount));
assert.deepEqual(clicked, ['[data-tab="queue"]']);
assert.equal(queueStatusFilter, 'failed');
assert.equal(queuePage, 1);
assert.equal(queueSearchTerm, term, 'the search must survive the trip');
// What the Queue then fetched back: only this user's failures.
let allQueueJobs = Array.from({length: Number(yours)}, (_, i) => ({id: i, status: 'failed'}));
let queueTotals = {failed: Number(yours), completed: 9};
const html = queueFailedFilterStatus();
assert(html.includes(expect), html);
if (unexpected !== 'None') assert(!html.includes(unexpected), html);
assert(html.includes('data-queue-clear-status'));
clearQueueStatusFilter();
assert.deepEqual(loads, [{status: '', term}], 'Show everything keeps the search');
assert.equal(queueStatsFailedCount, null);
"""
    result = subprocess.run(
        [node, "-e", script, str(stats_count), str(yours), term, expect, str(unexpected)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
