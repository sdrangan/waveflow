import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

interface FunctionSchema {
  function: { name: string; description: string; parameters: Record<string, unknown> };
}

interface Checkpoint {
  value: Record<string, unknown>;
  content: string;
}

/** Thin transport: Python owns schemas, candidate legality, evidence and budgets. */
export default async function firDse(pi: ExtensionAPI) {
  const python = process.env.WAVEFLOW_DSE_PYTHON || "python3";
  const prefix = ["-m", "examples.dse_fir"];
  // Host-owned configuration only: none of these values are tool arguments.
  if (process.env.WAVEFLOW_DSE_ROOT) prefix.push("--root", process.env.WAVEFLOW_DSE_ROOT);
  if (process.env.WAVEFLOW_DSE_CONFIG) prefix.push("--config", process.env.WAVEFLOW_DSE_CONFIG);
  const discovery = await pi.exec(python, [...prefix, "schemas"], {timeout: 30_000});
  if (discovery.code !== 0 || discovery.killed) {
    throw new Error(`FIR schema discovery failed: ${discovery.stderr || discovery.stdout}`);
  }
  const schemas: FunctionSchema[] = JSON.parse(discovery.stdout);
  const names = new Set(["dse_get_dse_context", "dse_pysim", "dse_predict_resource", "dse_synth", "dse_rtlsim", "dse_get_results"]);
  if (schemas.length !== names.size || new Set(schemas.map(s => s.function.name)).size !== names.size || schemas.some(s => !names.has(s.function.name))) {
    throw new Error("FIR service must export exactly the six bounded DSE tools");
  }
  const policy = process.env.WAVEFLOW_DSE_CONTEXT_POLICY ?? "pull";
  if (policy !== "pull" && policy !== "resume") throw new Error("Invalid FIR context policy; expected pull or resume");
  function readCheckpoint(stdout: string): Checkpoint {
    const context = JSON.parse(stdout);
    const content = context?.checkpoint_json;
    if (typeof content !== "string" || Buffer.byteLength(content, "utf8") > 8192) throw new Error("Invalid FIR checkpoint serialization");
    const cp = JSON.parse(content);
    if (context?.ok === false || !cp || cp.schema_version !== "fir-checkpoint-v1" || cp.policy_version !== "evidence-checkpoint-v1"
      || typeof cp.experiment_id !== "string" || !cp.experiment_id || typeof cp.checkpoint_id !== "string" || !cp.checkpoint_id
      || !Number.isSafeInteger(cp.revision) || cp.revision < 0
      || !["budget", "constraints", "best_feasible", "counts", "recent", "omitted_observations", "results_cursor", "semantics"].every(k => k in cp)
      || cp.experiment_id !== context.experiment_id) throw new Error("Invalid FIR context checkpoint");
    return {value: cp, content};
  }
  let pending: Checkpoint | undefined;
  let blocked: string | undefined = policy === "resume" ? "FIR session context is not validated" : undefined;
  let generation = 0;
  let sessionId: string | undefined;
  function bindCheckpoint(ctx: ExtensionContext, checkpoint: Record<string, unknown>) {
    // Read the whole session, not just its current branch: an experiment binding is session-wide.
    const bindings = ctx.sessionManager.getEntries().filter(e => e.type === "custom" && e.customType === "fir-dse-binding");
    if (bindings.some(e => (e as {data?: {experiment_id?: unknown}}).data?.experiment_id !== checkpoint.experiment_id)) {
      blocked = "FIR session experiment binding mismatch";
      pending = undefined;
      throw new Error(blocked);
    }
    if (!bindings.length) pi.appendEntry("fir-dse-binding", {experiment_id: checkpoint.experiment_id});
  }
  pi.on("session_shutdown", () => {
    generation++;
    sessionId = undefined;
    pending = undefined;
    blocked = "FIR session is shut down";
  });
  pi.on("session_start", async (_event, ctx) => {
    const currentGeneration = ++generation;
    const currentSession = ctx.sessionManager.getSessionId();
    sessionId = currentSession;
    pending = undefined;
    if (policy === "pull" && !ctx.sessionManager.getEntries().some(e => e.type === "custom" && e.customType === "fir-dse-binding")) {
      blocked = undefined;
      return;
    }
    blocked = "FIR session context is not validated";
    const response = await pi.exec(python, [...prefix, "call", "dse_get_dse_context", "--args", "{}"], {timeout: 30_000});
    if (currentGeneration !== generation || currentSession !== ctx.sessionManager.getSessionId()) return;
    if (response.code !== 0 || response.killed) throw new Error("FIR session context read failed");
    const checkpoint = readCheckpoint(response.stdout);
    bindCheckpoint(ctx, checkpoint.value);
    pending = policy === "resume" ? checkpoint : undefined;
    blocked = undefined;
  });
  function assertSession(ctx: ExtensionContext) {
    if (blocked) throw new Error(blocked);
    if (sessionId !== undefined && ctx.sessionManager.getSessionId() !== sessionId) throw new Error("FIR session changed; context is not validated");
  }
  function recordExposure(checkpoint: Checkpoint, source: string, content: string) {
    // Preserve numbers beyond JavaScript precision in canonical JSON, not a parsed copy.
    pi.appendEntry("fir-dse-exposure", {event: "submitted_for_context", source, policy,
      experiment_id: checkpoint.value.experiment_id, checkpoint_id: checkpoint.value.checkpoint_id,
      revision: checkpoint.value.revision, checkpoint_json: checkpoint.content, content});
  }
  pi.on("before_agent_start", (_event, ctx) => {
    // Pi reports hook errors but may continue its agent loop. execute() separately
    // enforces this guard; it does not sandbox the rest of the Pi host.
    assertSession(ctx);
    if (!pending) return;
    const checkpoint = pending;
    const content = checkpoint.content;
    recordExposure(checkpoint, "before_agent_start", content);
    pending = undefined;
    return {message: {customType: "fir-dse-checkpoint", content, display: true}};
  });
  for (const {function: tool} of schemas) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: Type.Unsafe<Record<string, unknown>>(tool.parameters),
      async execute(_toolCallId, params, signal, _onUpdate, ctx) {
        assertSession(ctx);
        const currentGeneration = generation;
        if (tool.name === "dse_get_dse_context") blocked = "FIR context read incomplete; restart session before further DSE calls";
        const response = await pi.exec(python, [...prefix, "call", tool.name, "--args", JSON.stringify(params)], {signal});
        if (response.killed || signal?.aborted) throw new Error("FIR call cancelled; query live results before retrying");
        try {
          JSON.parse(response.stdout);
        } catch {
          throw new Error(`FIR returned invalid JSON (exit ${response.code}): ${response.stderr}`);
        }
        if (tool.name === "dse_get_dse_context") {
          if (currentGeneration !== generation) throw new Error("FIR session changed during context read");
          try {
            if (response.code !== 0) throw new Error("FIR context read failed");
            const checkpoint = readCheckpoint(response.stdout);
            bindCheckpoint(ctx, checkpoint.value);
            recordExposure(checkpoint, "tool_result", response.stdout);
            blocked = undefined;
          } catch (error) {
            blocked = error instanceof Error ? error.message : "FIR context read failed";
            pending = undefined;
            throw error;
          }
        }
        // Preserve structured service failures as evidence rather than hiding their rows.
        return {content: [{type: "text", text: response.stdout}], details: {raw_json: response.stdout, transport_exit_code: response.code}};
      },
    });
  }
}
