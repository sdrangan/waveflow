import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

interface FunctionSchema {
  function: { name: string; description: string; parameters: Record<string, unknown> };
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
  for (const {function: tool} of schemas) {
    pi.registerTool({
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: Type.Unsafe<Record<string, unknown>>(tool.parameters),
      async execute(_toolCallId, params, signal) {
        const response = await pi.exec(python, [...prefix, "call", tool.name, "--args", JSON.stringify(params)], {signal});
        if (response.killed || signal?.aborted) throw new Error("FIR call cancelled; query live results before retrying");
        let details: Record<string, unknown>;
        try {
          details = JSON.parse(response.stdout);
        } catch {
          throw new Error(`FIR returned invalid JSON (exit ${response.code}): ${response.stderr}`);
        }
        // Preserve structured service failures as evidence rather than hiding their rows.
        return {content: [{type: "text", text: JSON.stringify(details)}], details: {...details, transport_exit_code: response.code}};
      },
    });
  }
}
