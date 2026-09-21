import assert from "node:assert/strict";
import { test } from "node:test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ExtensionAPI, ToolDefinition } from "@earendil-works/pi-coding-agent";
import extension from "./index.js";

const run = promisify(execFile);
test("registers six tools from real Python schemas and passes argv and cancellation", async () => {
  const tools: ToolDefinition[] = [];
  const calls: { command: string; args: string[]; signal?: AbortSignal }[] = [];
  const pi = {
    registerTool(tool: ToolDefinition) { tools.push(tool); },
    async exec(command: string, args: string[], options?: {signal?: AbortSignal}) {
      calls.push({command, args, signal: options?.signal});
      if (args.at(-1) === "schemas") {
        const {stdout, stderr} = await run(command, args);
        return {stdout, stderr, code: 0, killed: false};
      }
      return {stdout: '{"ok":true,"data":{}}', stderr: '', code: 0, killed: false};
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
});
