import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { serveRelay } from '../src/relay.js';

test('concurrent requests and a relay restart reuse the same real Chrome target', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'obx-relay-'));
  const targets = new Map<string, object>();
  let created = 0;
  let holdNextList = false;
  let releaseList: (() => void) | undefined;
  const chrome = createServer((req, res) => {
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/json/version') return res.end(JSON.stringify({ webSocketDebuggerUrl: 'ws://127.0.0.1/browser/stable' }));
    if (req.url === '/json/list') {
      const response = JSON.stringify([...targets.values()]);
      if (holdNextList) {
        holdNextList = false; releaseList = () => res.end(response); return;
      }
      return res.end(response);
    }
    if (req.url?.startsWith('/json/new')) {
      const id = `target-${++created}`;
      const target = { id, type: 'page', url: 'about:blank' };
      targets.set(id, target); return res.end(JSON.stringify(target));
    }
    if (req.url?.startsWith('/json/close/')) {
      targets.delete(req.url.slice('/json/close/'.length)); return res.end(JSON.stringify(true));
    }
    res.statusCode = 404; res.end('{}');
  });
  await new Promise<void>(resolve => chrome.listen(0, '127.0.0.1', resolve));
  const chromePort = (chrome.address() as { port: number }).port;
  // Reserve an available relay port, then reuse it across the process restart.
  const reservation = createServer();
  await new Promise<void>(resolve => reservation.listen(0, '127.0.0.1', resolve));
  const port = (reservation.address() as { port: number }).port;
  await new Promise<void>(resolve => reservation.close(() => resolve()));
  let relay = await serveRelay({ mode: 'local', port, chromePort, stateDirectory: dir });
  const base = `http://127.0.0.1:${port}`;
  const getPage = async (name = 'laike') => {
    const response = await fetch(`${base}/pages`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });
    assert.equal(response.status, 200); return response.json() as Promise<{ targetId: string }>;
  };
  try {
    const pages = await Promise.all([getPage(), getPage(), getPage()]);
    assert.equal(created, 1);
    assert.ok(pages.every(p => p.targetId === 'target-1'));
    await relay.stop();
    relay = await serveRelay({ mode: 'auto', port, chromePort, stateDirectory: dir });
    assert.deepEqual(await (await fetch(`${base}/pages`)).json(), { pages: ['laike'] });
    assert.equal((await getPage()).targetId, 'target-1');
    assert.equal(created, 1);
    assert.equal((await fetch(`${base}/pages/laike`, { method: 'DELETE' })).status, 200);
    assert.equal(targets.size, 0);
    holdNextList = true;
    const staleList = fetch(`${base}/pages`);
    while (!releaseList) await new Promise(resolve => setTimeout(resolve, 1));
    const concurrent = await getPage('new-page');
    releaseList();
    await staleList;
    assert.deepEqual(await (await fetch(`${base}/pages`)).json(), { pages: ['new-page'] });
    assert.equal((await getPage('new-page')).targetId, concurrent.targetId);
    assert.equal(created, 2);
  } finally {
    await relay.stop();
    chrome.closeAllConnections();
    await new Promise<void>(resolve => chrome.close(() => resolve()));
    rmSync(dir, { recursive: true, force: true });
  }
});
