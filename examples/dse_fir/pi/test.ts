import assert from "node:assert/strict";
import { test } from "node:test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { createAgentSession, DefaultResourceLoader, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import extension from "./index.js";

const run = promisify(execFile);
const checkpoint = {schema_version: "fir-checkpoint-v1", policy_version: "evidence-checkpoint-v1", experiment_id: "experiment-a", revision: 0, checkpoint_id: "checkpoint-a", budget: {}, constraints: {}, best_feasible: null, counts: {}, recent: [], omitted_observations: 0, results_cursor: 0, semantics: {}};
async function harness(policy = "resume", entries: any[] = []) {
  process.env.WAVEFLOW_DSE_CONTEXT_POLICY = policy;
  const handlers = new Map<string, Function>();
  const tools: ToolDefinition[] = [];
  const calls: string[][] = [];
  const ctx = {sessionManager: {getSessionId: () => "session-a", getEntries: () => entries}};
  let response: () => Promise<unknown> = async () => ({checkpoint});
  const pi = {
    on(name: string, handler: Function) {handlers.set(name, handler);},
    appendEntry(customType: string, data: unknown) {entries.push({type: "custom", customType, data});},
    registerTool(tool: ToolDefinition) {tools.push(tool);},
    async exec(command: string, args: string[]) {
      calls.push(args);
      const value = args.at(-1) === "schemas" ? undefined : await response() as any;
      const stdout = value === undefined ? (await run(command, args)).stdout : JSON.stringify({...value, experiment_id: value.checkpoint?.experiment_id, checkpoint: undefined, checkpoint_json: value.checkpoint ? JSON.stringify(value.checkpoint) : undefined});
      return {stdout, stderr: "", code: 0, killed: false};
    },
  } as unknown as ExtensionAPI;
  try {await extension(pi);} finally {delete process.env.WAVEFLOW_DSE_CONTEXT_POLICY;}
  return {tools, entries, calls, ctx, respond(fn: typeof response) {response = fn;},
    async emit(name: string, context = ctx) {return await handlers.get(name)?.({type: name, reason: "resume"}, context);}};
}

test("resume stages one domain checkpoint and records exact submitted exposure", async () => {
  const h = await harness();
  await h.emit("session_start");
  assert.ok(h.entries.some(e => e.customType === "fir-dse-binding" && e.data.experiment_id === checkpoint.experiment_id));
  assert.equal(h.entries.filter(e => e.customType === "fir-dse-exposure").length, 0);
  const result = await h.emit("before_agent_start");
  assert.equal(result.message.customType, "fir-dse-checkpoint");
  assert.equal(result.message.content, JSON.stringify(checkpoint));
  const exposure = h.entries.find(e => e.customType === "fir-dse-exposure").data;
  assert.equal(exposure.event, "submitted_for_context");
  assert.deepEqual(JSON.parse(exposure.checkpoint_json), checkpoint);
  assert.equal(exposure.content, result.message.content);
  assert.equal(await h.emit("before_agent_start"), undefined);
});
test("mismatched persisted binding fails closed, including DSE execution", async () => {
  const h = await harness("resume", [{type: "custom", customType: "fir-dse-binding", data: {experiment_id: "other"}}]);
  await assert.rejects(h.emit("session_start"), /binding/i);
  await assert.rejects(h.emit("before_agent_start"), /binding/i);
  const count = h.calls.length;
  await assert.rejects(h.tools[0].execute("id", {}, undefined, undefined, h.ctx as never), /binding/i);
  assert.equal(h.calls.length, count);
  assert.equal(h.entries.length, 1);
});

test("rejects invalid policy and invalid context before exposure", async () => {
  await assert.rejects(harness("invalid"), /policy/i);
  for (const value of [{ok: false}, {checkpoint: {...checkpoint, revision: "0"}}, {checkpoint: {...checkpoint, semantics: "é".repeat(8192)}}]) {
    const h = await harness();
    h.respond(async () => value);
    await assert.rejects(h.emit("session_start"), /context|checkpoint/i);
    await assert.rejects(h.emit("before_agent_start"), /context|checkpoint/i);
    await assert.rejects(h.tools[0].execute("id", {}, undefined, undefined, h.ctx as never), /context|checkpoint/i);
    assert.equal(h.entries.length, 0);
  }
});

test("shutdown discards pending injection and stale asynchronous session reads", async () => {
  const h = await harness();
  let finish!: (value: unknown) => void;
  h.respond(() => new Promise(resolve => {finish = resolve;}));
  const stale = h.emit("session_start");
  await h.emit("session_shutdown");
  h.respond(async () => ({checkpoint: {...checkpoint, experiment_id: "replacement", checkpoint_id: "replacement"}}));
  await h.emit("session_start");
  finish({checkpoint});
  await stale;
  const result = await h.emit("before_agent_start");
  assert.equal(JSON.parse(result.message.content).experiment_id, "replacement");
  assert.equal(h.entries.filter(e => e.customType === "fir-dse-binding").length, 1);
  await h.emit("session_start");
  await h.emit("session_shutdown");
  await assert.rejects(h.emit("before_agent_start"), /session/i);
});

test("pull does not inject; explicit context records exposure and binds the session", async () => {
  const h = await harness("pull");
  await h.emit("session_start");
  assert.equal(h.calls.length, 1);
  assert.equal(await h.emit("before_agent_start"), undefined);
  const result = await h.tools.find(t => t.name === "dse_get_dse_context")!.execute("context-id", {}, undefined, undefined, h.ctx as never);
  assert.equal(result.content[0].type, "text");
  assert.equal(h.entries.find(e => e.customType === "fir-dse-binding").data.experiment_id, checkpoint.experiment_id);
  const exposure = h.entries.find(e => e.customType === "fir-dse-exposure").data;
  assert.equal(exposure.event, "submitted_for_context");
  assert.equal(exposure.source, "tool_result");
  assert.deepEqual(JSON.parse(exposure.checkpoint_json), checkpoint);
  assert.equal(exposure.content, result.content[0].type === "text" ? result.content[0].text : "");
  await h.emit("session_shutdown");
  h.respond(async () => ({checkpoint: {...checkpoint, experiment_id: "other"}}));
  await assert.rejects(h.emit("session_start"), /binding/i);
});

test("real Pi SDK persists and restores checkpoint entries with real Python CLI, without inference", async () => {
  const dir = await mkdtemp(join(tmpdir(), "fir-pi-sdk-"));
  const saved = {root: process.env.WAVEFLOW_DSE_ROOT, policy: process.env.WAVEFLOW_DSE_CONTEXT_POLICY, config: process.env.WAVEFLOW_DSE_CONFIG};
  process.env.WAVEFLOW_DSE_ROOT = join(dir, "domain");
  process.env.WAVEFLOW_DSE_CONTEXT_POLICY = "resume";
  process.env.WAVEFLOW_DSE_CONFIG = join(dir, "config.json");
  await writeFile(process.env.WAVEFLOW_DSE_CONFIG, '{"constraints":{"max_top_lut":9007199254740993}}');
  const sessions: Awaited<ReturnType<typeof createAgentSession>>["session"][] = [];
  const errors: string[] = [];
  async function open(manager: SessionManager) {
    const settingsManager = SettingsManager.inMemory({packages: [], compaction: {enabled: false}});
    const resourceLoader = new DefaultResourceLoader({cwd: dir, agentDir: join(dir, "agent"), settingsManager,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true, extensionFactories: [extension]});
    await resourceLoader.reload();
    assert.deepEqual(resourceLoader.getExtensions().errors, []);
    const {session} = await createAgentSession({cwd: dir, agentDir: join(dir, "agent"), settingsManager, resourceLoader, sessionManager: manager, tools: []});
    sessions.push(session);
    await session.bindExtensions({onError: e => errors.push(e.error)});
    return session;
  }
  try {
    const manager = SessionManager.create(dir, join(dir, "sessions"));
    // Pi writes a new session file only after its first assistant message. This fixture is not inference evidence.
    manager.appendMessage({role: "assistant", content: [{type: "text", text: "offline fixture"}], api: "openai-responses", provider: "openai", model: "offline", usage: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0}}, stopReason: "stop", timestamp: 0});
    const first = await open(manager);
    assert.deepEqual(errors, []);
    const result = await first.extensionRunner!.emitBeforeAgentStart("offline lifecycle test", undefined, {cwd: dir});
    assert.equal(result.messages?.length, 1);
    const message = result.messages![0];
    assert.equal(message.customType, "fir-dse-checkpoint");
    // Exercise the canonical persistence API, without submitting a prompt/provider request.
    manager.appendCustomMessageEntry(message.customType, message.content, message.display, message.details);
    const before = JSON.parse(message.content as string);
    assert.equal(before.schema_version, "fir-checkpoint-v1");
    const verifyDigest = "import json,sys; from examples.dse_fir.contracts import identity; cp=json.loads(sys.argv[1]); digest=cp.pop('checkpoint_id'); assert identity(cp)==digest, 'checkpoint digest changed'; assert cp['constraints']['max_top_lut']==9007199254740993";
    await run(process.env.WAVEFLOW_DSE_PYTHON || "python3", ["-c", verifyDigest, message.content as string]);
    await first.extensionRunner!.emit({type: "session_shutdown", reason: "quit"});
    first.dispose();
    const restored = SessionManager.open(manager.getSessionFile()!, join(dir, "sessions"));
    assert.ok(restored.getEntries().some(e => e.type === "custom" && e.customType === "fir-dse-exposure"));
    assert.ok(restored.buildSessionContext().messages.some(m => m.role === "custom" && m.customType === "fir-dse-checkpoint"));
    const second = await open(restored);
    const resumed = await second.extensionRunner!.emitBeforeAgentStart("offline resume test", undefined, {cwd: dir});
    assert.deepEqual(errors, []);
    assert.deepEqual(JSON.parse(resumed.messages![0].content as string), before);
    await run(process.env.WAVEFLOW_DSE_PYTHON || "python3", ["-c", verifyDigest, resumed.messages![0].content as string]);
    assert.equal((await second.extensionRunner!.emitBeforeAgentStart("next turn", undefined, {cwd: dir})).messages?.length ?? 0, 0);
    const entries = restored.getEntries().filter(e => e.type === "custom" && e.customType === "fir-dse-exposure");
    assert.equal(entries.length, 2);
    assert.equal(restored.getEntries().filter(e => e.type === "custom" && e.customType === "fir-dse-binding").length, 1);
  } finally {
    for (const session of sessions) session.dispose();
    if (saved.root === undefined) delete process.env.WAVEFLOW_DSE_ROOT; else process.env.WAVEFLOW_DSE_ROOT = saved.root;
    if (saved.policy === undefined) delete process.env.WAVEFLOW_DSE_CONTEXT_POLICY; else process.env.WAVEFLOW_DSE_CONTEXT_POLICY = saved.policy;
    if (saved.config === undefined) delete process.env.WAVEFLOW_DSE_CONFIG; else process.env.WAVEFLOW_DSE_CONFIG = saved.config;
    await rm(dir, {recursive: true, force: true});
  }
});

test("replacement session cannot consume pending checkpoint or run old bound tools", async () => {
  const h = await harness();
  await h.emit("session_start");
  const replacement = {sessionManager: {...h.ctx.sessionManager, getSessionId: () => "session-b"}};
  await assert.rejects(h.emit("before_agent_start", replacement), /session/i);
  await assert.rejects(h.tools[0].execute("id", {}, undefined, undefined, replacement as never), /session/i);
  assert.equal(h.entries.filter(e => e.customType === "fir-dse-exposure").length, 0);
});

test("pull context transport failure blocks subsequent DSE tools", async () => {
  const h = await harness("pull");
  await h.emit("session_start");
  h.respond(async () => {throw new Error("context transport unavailable");});
  await assert.rejects(h.tools.find(t => t.name === "dse_get_dse_context")!.execute("id", {}, undefined, undefined, h.ctx as never), /context/i);
  h.respond(async () => ({ok: true}));
  const count = h.calls.length;
  await assert.rejects(h.tools.find(t => t.name === "dse_pysim")!.execute("id", {}, undefined, undefined, h.ctx as never), /context/i);
  assert.equal(h.calls.length, count);
});

test("registers six tools from real Python schemas and passes argv and cancellation", async () => {
  const tools: ToolDefinition[] = [];
  const calls: { command: string; args: string[]; signal?: AbortSignal }[] = [];
  const pi = {
    on() {},
    registerTool(tool: ToolDefinition) { tools.push(tool); },
    async exec(command: string, args: string[], options?: {signal?: AbortSignal}) {
      calls.push({command, args, signal: options?.signal});
      if (args.at(-1) === "schemas") {
        const {stdout, stderr} = await run(command, args);
        return {stdout, stderr, code: 0, killed: false};
      }
      return {stdout: '{"ok":true,"data":{"integer":9007199254740993,"float":20.0}}', stderr: '', code: 0, killed: false};
    },
  } as unknown as ExtensionAPI;
  await extension(pi);
  assert.equal(tools.length, 6);
  assert.deepEqual(tools.map(t => t.name).sort(), ["dse_get_dse_context", "dse_get_results", "dse_predict_resource", "dse_pysim", "dse_rtlsim", "dse_synth"]);
  const signal = new AbortController().signal;
  const params = {params: {untrusted: "'; $(touch /bad); '"}};
  const result = await tools.find(t => t.name === "dse_pysim")!.execute("id", params, signal, undefined, {} as never);
  assert.equal(calls.at(-1)?.signal, signal);
  assert.equal(calls.at(-1)?.args.at(-1), JSON.stringify(params));
  assert.equal(result.content[0].type, "text");
  assert.equal(result.content[0].type === "text" && result.content[0].text, '{"ok":true,"data":{"integer":9007199254740993,"float":20.0}}');
  assert.deepEqual(result.details, {raw_json: '{"ok":true,"data":{"integer":9007199254740993,"float":20.0}}', transport_exit_code: 0});
});
