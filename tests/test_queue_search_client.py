"""Exercise queue polling with delayed responses, without touching the library."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("next_term", ["new", ""])
def test_changed_search_discards_old_response_and_fetches_latest(outcome, next_term):
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
let queueLoadInFlight = false, queueSearchTerm = 'old', allQueueJobs = [];
let queueTotals = null, queueLastUpdatedAt = 0, queuePage = 1;
const QUEUE_SEARCH_LIMIT = 250, QUEUE_PAGE_SIZE = 10;
const queueTab = {innerHTML: ''};
const pending = [], rendered = [];
const apiFetch = url => new Promise((resolve, reject) => pending.push({url, resolve, reject}));
const renderQueueSummary = () => {};
const renderQueuePage = () => rendered.push({term: queueSearchTerm, jobs: allQueueJobs});
eval(source.slice(start, end));
(async () => {
    const first = loadJobs();
    queueSearchTerm = process.argv[2];
    await loadJobs(false);
    assert.equal(pending.length, 1, 'requests should stay serial');
    if (process.argv[1] === 'failure') pending[0].reject(new Error('old request failed'));
    else pending[0].resolve({ok: true, json: async () => ({jobs: [{id: 'old'}]})});
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(rendered, [], 'outdated results must never render');
    assert(!queueTab.innerHTML.includes('Failed to load queue'));
    assert.equal(pending.length, 2, 'latest search must run as soon as the old request finishes');
    assert.equal(pending[1].url, '/api/jobs?limit=250' + (queueSearchTerm ? '&q=new' : ''));
    pending[1].resolve({ok: true, json: async () => ({jobs: [{id: 'latest'}], queue_totals: {completed: 1}})});
    await first;
    assert.deepEqual(rendered, [{term: queueSearchTerm, jobs: [{id: 'latest'}]}]);
    assert.equal(queueLoadInFlight, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "-e", script, outcome, next_term],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
