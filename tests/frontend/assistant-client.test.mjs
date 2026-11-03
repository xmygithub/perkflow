import test from 'node:test';
import assert from 'node:assert/strict';
import { consumeEvents, createAssistantClient } from '../../frontend/src/assistant-client.js';

function stream(chunks) {
  return new ReadableStream({ start(controller) {
    for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
    controller.close();
  } });
}
test('NDJSON progress tolerates chunk boundaries and requires authoritative done', async () => {
  const progress = [];
  const result = await consumeEvents(stream([
    '{"type":"progress","event":{"text":"查询', '团购","kind":"tool_start"}}\n',
    '{"type":"done","result":{"answer":"完成","conversation":{"id":"one","version":1}}}\n',
  ]), event => progress.push(event));
  assert.equal(progress[0].text, '查询团购');
  assert.equal(result.conversation.version, 1);
  await assert.rejects(consumeEvents(stream(['{"type":"progress","event":{"text":"查询"}}\n']), () => {}), /连接已中断/);
});
test('stream errors and malformed data cannot become successful answers', async () => {
  await assert.rejects(consumeEvents(stream(['{"type":"error","code":"AGENT_TIMEOUT","message":"超时"}\n']), () => {}), /超时/);
  await assert.rejects(consumeEvents(stream(['not-json\n']), () => {}), /格式/);
});
test('turn carries session and CSRF, sends only message and server version', async () => {
  const calls = [];
  const client = createAssistantClient({ csrfToken: async () => 'csrf' }, async (url, args) => {
    calls.push({ url, args });
    return new Response('{"type":"done","result":{"answer":"请补充日期","conversation":{"id":"one","version":1}}}\n');
  });
  const result = await client.turn({ id: 'one', version: 0 }, '两个人', () => {});
  assert.equal(result.answer, '请补充日期');
  assert.equal(calls[0].args.credentials, 'include');
  assert.equal(calls[0].args.headers['X-CSRF-Token'], 'csrf');
  assert.deepEqual(JSON.parse(calls[0].args.body), { message: '两个人', expected_version: 0 });
  assert.equal(calls.length, 1);
});

test('purchase confirmation is explicit, carries no client amount, and never auto retries', async () => {
  const calls = [];
  const client = createAssistantClient({ csrfToken: async () => 'csrf' }, async (url, args) => {
    calls.push({url, args});
    if (args.method === 'POST') throw new TypeError('response lost');
    return new Response('[]');
  });
  await assert.rejects(client.confirm({id:'one',version:3}, 'proposal', true), /response lost/);
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].args.body), {proposal_id:'proposal',expected_version:3,acknowledge_unknowns:true});
  assert.equal(calls[0].args.headers['X-CSRF-Token'], 'csrf');
  await client.purchases('one');
  assert.equal(calls[1].args.method, 'GET');
  assert.equal(calls.length, 2);
});

test('purchase lookup has an abort signal and reports timeout without a retry or purchase', async () => {
  const calls = [];
  const client = createAssistantClient({}, async (url, args) => {
    calls.push({url, args});
    assert.ok(args.signal instanceof AbortSignal);
    throw new DOMException('aborted', 'AbortError');
  });
  await assert.rejects(client.purchases('one'), error => error.code === 'PURCHASE_LOOKUP_TIMEOUT' && /回查超时/.test(error.message));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].args.method, 'GET');
});

test('account and exact purchase recovery send bounded GETs without user identity or confirmation', async () => {
  const calls = [];
  const client = createAssistantClient({}, async (url, args) => {
    calls.push({url, args}); return new Response('[]');
  });
  await client.accountPurchases(2, 'unresolved');
  await client.purchases('chat/one', 'old/quote');
  assert.equal(calls[0].url, '/api/assistant/purchases?page=2&page_size=5&scope=unresolved');
  assert.equal(calls[1].url, '/api/assistant/conversations/chat%2Fone/purchases?purchase_id=old%2Fquote');
  assert.ok(calls.every(call => call.args.method === 'GET' && call.args.credentials === 'include' && call.args.signal instanceof AbortSignal));
  assert.ok(calls.every(call => !call.args.body && !call.args.headers['X-CSRF-Token']));
});
