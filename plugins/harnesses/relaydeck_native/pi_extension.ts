/**
 * relaydeck fleet tools for pi — message peers, manage agents, reshape dashboard.
 *
 * Loaded via `pi -e …/pi_extension.ts`. Enabled tools are gated by the
 * RELAYDECK_TOOLS env var (comma-separated capability names set by pi_engine).
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

function enabled(name: string): boolean {
	const raw = process.env.RELAYDECK_TOOLS || "";
	return raw.split(",").map((s) => s.trim()).filter(Boolean).includes(name);
}

async function relaydeck(args: string[]): Promise<string> {
	const ws = (process.env.RELAYDECK_WORKSPACE || "").trim();
	// Only append workspace flags where the subcommand accepts them.
	// NEVER append `-w` globally — `workspace message` only accepts
	// `--workspace`, and blind `-w` breaks messaging (Click: "No such option").
	const full = [...args, ...workspaceSuffix(args, ws)];
	try {
		const { stdout, stderr } = await execFileAsync("relaydeck", full, {
			env: relaydeckEnv(ws),
			maxBuffer: 4 * 1024 * 1024,
		});
		return ((stdout || "") + (stderr || "")).trim() || "(ok)";
	} catch (err: any) {
		const out = String(err?.stdout || "") + String(err?.stderr || "");
		return out.trim() || String(err?.message || err);
	}
}

/** Subcommands that accept an explicit workspace scope flag. */
function workspaceSuffix(args: string[], ws: string): string[] {
	if (!ws) return [];
	const cmd = args[0] || "";
	const sub = args[1] || "";
	if (cmd === "theme" && (sub === "set" || sub === "appearance")) {
		return ["--workspace", ws];
	}
	if (cmd === "dashboard" && sub === "get") {
		return ["-w", ws];
	}
	return [];
}

/** Minimal env for relaydeck CLI spawns — omit provider keys pi inherited. */
function relaydeckEnv(workspace: string): NodeJS.ProcessEnv {
	const keep = new Set([
		"PATH", "HOME", "USER", "LANG", "LC_ALL",
		"RELAYDECK_AGENT_ID", "RELAYDECK_WORKSPACE", "RELAYDECK_CONFIG_HOME", "RELAYDECK_TOOLS",
	]);
	const env: NodeJS.ProcessEnv = {};
	for (const [key, val] of Object.entries(process.env)) {
		if (val === undefined) continue;
		if (keep.has(key)) {
			env[key] = val;
			continue;
		}
		if (key.endsWith("_API_KEY")) continue;
		if (key.startsWith("RELAYDECK_") && key.endsWith("_BASE_URL")) continue;
		env[key] = val;
	}
	if (workspace) env.RELAYDECK_WORKSPACE = workspace;
	return env;
}

const MESSAGE_PARAMS = Type.Object({
	to: Type.String({ description: "Peer agent id" }),
	body: Type.String({ description: "Message body" }),
});

const AGENT_ID_PARAMS = Type.Object({
	agent: Type.String({ description: "Target agent id" }),
});

const DASHBOARD_PARAMS = Type.Object({
	op: Type.String({
		description:
			"Dashboard op: get (appearance + widget grid), theme (base/daylight=light, ink/gruvbox-dark=dark), density, glow, add, remove, move, resize, tidy, reset",
	}),
	value: Type.Optional(Type.String({ description: "Theme id, widget name, or scalar value" })),
	x: Type.Optional(Type.Number()),
	y: Type.Optional(Type.Number()),
	w: Type.Optional(Type.Number()),
	h: Type.Optional(Type.Number()),
});

export default function relaydeckFleet(pi: ExtensionAPI) {
	const fromId = process.env.RELAYDECK_AGENT_ID || "relaydeck";

	if (enabled("relaydeck")) {
		pi.registerTool({
			name: "relaydeck_message",
			label: "Message peer",
			description: "Send a durable message to another relaydeck agent in the fleet",
			parameters: MESSAGE_PARAMS,
			async execute(_id, params) {
				const out = await relaydeck([
					"workspace",
					"message",
					params.body,
					"--agent",
					params.to,
				]);
				return { content: [{ type: "text", text: out }], details: { from: fromId, to: params.to } };
			},
		});
	}

	if (enabled("manage")) {
		pi.registerTool({
			name: "relaydeck_agents",
			label: "List agents",
			description: "List agents in this agent's workspace",
			parameters: Type.Object({}),
			async execute() {
				const out = await relaydeck(["agent", "list"]);
				return { content: [{ type: "text", text: out }] };
			},
		});
		pi.registerTool({
			name: "relaydeck_start",
			label: "Start agent",
			description: "Start a peer agent in the same workspace",
			parameters: AGENT_ID_PARAMS,
			async execute(_id, params) {
				if (params.agent === fromId) {
					return { content: [{ type: "text", text: "refusing to start self" }], details: { error: true } };
				}
				const out = await relaydeck(["agent", "start", params.agent]);
				return { content: [{ type: "text", text: out }] };
			},
		});
		pi.registerTool({
			name: "relaydeck_stop",
			label: "Stop agent",
			description: "Stop a peer agent in the same workspace",
			parameters: AGENT_ID_PARAMS,
			async execute(_id, params) {
				if (params.agent === fromId) {
					return { content: [{ type: "text", text: "refusing to stop self" }], details: { error: true } };
				}
				const out = await relaydeck(["agent", "stop", params.agent]);
				return { content: [{ type: "text", text: out }] };
			},
		});
	}

	if (enabled("dashboard")) {
		pi.registerTool({
			name: "relaydeck_dashboard",
			label: "Dashboard",
			description:
				"Read or reshape the live relaydeck web dashboard. op=get lists theme, density, glow, and the saved Home widget grid.",
			parameters: DASHBOARD_PARAMS,
			async execute(_id, params) {
				const op = params.op;
				if (op === "get") {
					const out = await relaydeck(["dashboard", "get"]);
					return { content: [{ type: "text", text: out }] };
				}
				if (op === "theme" && params.value) {
					// Persist server-side (workspace-scoped when RELAYDECK_WORKSPACE set).
					const out = await relaydeck(["theme", "set", String(params.value)]);
					return { content: [{ type: "text", text: out }] };
				}
				const args = ["dashboard", op];
				if (params.value) args.push(String(params.value));
				if (op === "move") {
					args.push(String(params.x ?? 0), String(params.y ?? 0));
				}
				if (op === "resize") {
					args.push(String(params.w ?? 4), String(params.h ?? 3));
				}
				const out = await relaydeck(args);
				return { content: [{ type: "text", text: out }] };
			},
		});
	}
}
