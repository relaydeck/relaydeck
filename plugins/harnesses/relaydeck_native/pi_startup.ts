/**
 * relaydeck-native startup UX — custom header, footer, and module banner.
 *
 * Loaded only for relaydeck-managed pi children (TERM_PROGRAM=relaydeck-native).
 * Replaces the default pi startup chrome with fleet context from RELAYDECK_STARTUP.
 */

import type { AssistantMessage } from "@mariozechner/pi-ai";
import type { ExtensionAPI, Theme } from "@mariozechner/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@mariozechner/pi-tui";

type StartupBundle = {
	identity?: string;
	agent_id?: string;
	workspace?: string | null;
	model?: string;
	tools?: string[];
	pi_tools?: string[];
	extensions?: string[];
	plugins?: string[];
	prompt_layers?: string[];
	skills?: number;
	fleet?: { agents_total?: number; agents_running?: number; workers?: number };
	appearance?: { theme?: string; density?: string; scope?: string };
	pi_version?: string | null;
};

function isNative(): boolean {
	return (process.env.TERM_PROGRAM || "").trim() === "relaydeck-native";
}

function loadStartup(): StartupBundle {
	const raw = (process.env.RELAYDECK_STARTUP || "").trim();
	if (!raw) return {};
	try {
		return JSON.parse(raw) as StartupBundle;
	} catch {
		return {};
	}
}

function accent(theme: Theme, text: string): string {
	return theme.fg("accent", text);
}

function dim(theme: Theme, text: string): string {
	return theme.fg("dim", text);
}

function muted(theme: Theme, text: string): string {
	return theme.fg("muted", text);
}

function headerLines(theme: Theme, st: StartupBundle, width: number): string[] {
	const agent = st.agent_id || process.env.RELAYDECK_AGENT_ID || "agent";
	const ws = st.workspace || process.env.RELAYDECK_WORKSPACE || "";
	const model = st.model || "—";
	const fleet = st.fleet || {};
	const bar = accent(theme, "═".repeat(Math.min(44, Math.max(20, width - 4))));
	const title = accent(theme, "◆ relaydeck-native");
	const sub = muted(
		theme,
		truncateToWidth(`${agent}${ws ? ` · @${ws}` : ""} · ${model}`, Math.max(20, width - 4)),
	);
	const stats = dim(
		theme,
		`${fleet.agents_running ?? 0}/${fleet.agents_total ?? 0} agents`
		+ (fleet.workers ? ` · ${fleet.workers} workers` : "")
		+ " · /modules for loaded stack",
	);
	const tag = dim(theme, "managed pi session — not your personal ~/.pi install");
	return ["", bar, title, sub, stats, tag, bar, ""];
}

function startupPanel(theme: Theme, st: StartupBundle, toolNames: string[]): string[] {
	const lines: string[] = [];
	lines.push(accent(theme, "STARTUP · relaydeck-native"));
	lines.push(dim(theme, "─".repeat(40)));

	lines.push(muted(theme, "extensions"));
	for (const ext of st.extensions || ["relaydeck-startup", "relaydeck-fleet"]) {
		lines.push(`  ${accent(theme, "●")} ${ext}`);
	}

	lines.push("");
	lines.push(muted(theme, "tools"));
	const listed = toolNames.length ? toolNames : st.pi_tools || [];
	if (listed.length) {
		for (const t of listed) lines.push(`  ${dim(theme, "·")} ${t}`);
	} else {
		lines.push(`  ${dim(theme, "(none enabled)")}`);
	}

	if (st.plugins?.length) {
		lines.push("");
		lines.push(muted(theme, `workspace plugins @ ${st.workspace || "—"}`));
		for (const p of st.plugins) lines.push(`  ${dim(theme, "·")} ${p}`);
	}

	if (st.prompt_layers?.length) {
		lines.push("");
		lines.push(muted(theme, "prompt layers"));
		for (const ly of st.prompt_layers) lines.push(`  ${dim(theme, "·")} ${ly}`);
	}

	const fleet = st.fleet || {};
	const appearance = st.appearance || {};
	lines.push("");
	lines.push(muted(theme, "fleet"));
	lines.push(
		`  agents  ${fleet.agents_running ?? 0} running / ${fleet.agents_total ?? 0} total`
		+ `   workers ${fleet.workers ?? 0}`,
	);
	lines.push(
		`  theme   ${appearance.theme || "base"}`
		+ (appearance.density ? ` · ${appearance.density}` : "")
		+ (appearance.scope ? ` (${appearance.scope})` : ""),
	);
	if (typeof st.skills === "number" && st.skills > 0) {
		lines.push(`  skills  ${st.skills} injectable`);
	}
	if (st.pi_version) {
		lines.push(`  pi      ${dim(theme, st.pi_version)}`);
	}
	lines.push("");
	return lines;
}

function tokenStats(ctx: { sessionManager: { getBranch(): Iterable<{ type: string; message?: { role?: string; usage?: AssistantMessage["usage"] } }> } }): {
	in: number;
	out: number;
	cost: number;
} {
	let input = 0;
	let output = 0;
	let cost = 0;
	for (const e of ctx.sessionManager.getBranch()) {
		if (e.type === "message" && e.message.role === "assistant") {
			const m = e.message as AssistantMessage;
			input += m.usage?.input ?? 0;
			output += m.usage?.output ?? 0;
			cost += m.usage?.cost?.total ?? 0;
		}
	}
	return { in: input, out: output, cost };
}

function fmt(n: number): string {
	return n < 1000 ? `${n}` : `${(n / 1000).toFixed(1)}k`;
}

export default function relaydeckStartup(pi: ExtensionAPI) {
	if (!isNative()) return;

	const startup = loadStartup();

	pi.on("session_start", async (_event, ctx) => {
		if (!ctx.hasUI) return;

		ctx.ui.setTitle(`relaydeck · ${startup.agent_id || "native"}`);
		ctx.ui.setHeader((_tui, theme) => ({
			render(width: number): string[] {
				return headerLines(theme, startup, width);
			},
			invalidate() {},
		}));

		ctx.ui.setFooter((_tui, theme, _footerData) => ({
			invalidate() {},
			render(width: number): string[] {
				const stats = tokenStats(ctx);
				const model = ctx.model?.id || startup.model || "no-model";
				const ws = startup.workspace ? `@${startup.workspace}` : "";
				const fleet = startup.fleet || {};
				const left = dim(
					theme,
					`↑${fmt(stats.in)} ↓${fmt(stats.out)} $${stats.cost.toFixed(3)}`,
				);
				const right = muted(
					theme,
					`${model}${ws ? ` · ${ws}` : ""} · ${fleet.agents_running ?? 0}↑`,
				);
				const pad = " ".repeat(Math.max(1, width - visibleWidth(left) - visibleWidth(right)));
				return [truncateToWidth(left + pad + right, width)];
			},
		}));

		const tools = pi.getAllTools().map((t) => t.name).sort();
		ctx.ui.setWidget("relaydeck-startup", startupPanel(themeFromCtx(ctx), startup, tools), {
			placement: "aboveEditor",
		});
	});

	pi.on("turn_start", (_event, ctx) => {
		if (ctx.hasUI) ctx.ui.setWidget("relaydeck-startup", undefined);
	});

	pi.registerCommand("modules", {
		description: "Show relaydeck-native startup modules and fleet stats",
		handler: async (_args, ctx) => {
			const tools = pi.getAllTools().map((t) => t.name).sort();
			const lines = startupPanel(themeFromCtx(ctx), startup, tools);
			ctx.ui.setWidget("relaydeck-startup", lines, { placement: "aboveEditor" });
		},
	});
}

function themeFromCtx(ctx: { ui: { theme: Theme } }): Theme {
	return ctx.ui.theme;
}
