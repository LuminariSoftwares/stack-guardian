// @job: wq1004-sg-dsh (P43 S6, C-DSH) -- ox try 1 via the work queue; probe 9/9 on Windows; contract + probe by O5 2026-10-04
// stack-guardian DSH plugin (P43 S6, C-DSH): one start-of-session line when the stack doctor reports WARN/FAIL.
// agent/session-start starts `stack-guardian doctor --json --quiet`; the first agent/pre-step adds the line
// (within budgetMs); OK/INFO, a timeout or a bad report add nothing. Zero DSH imports.
import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";


export const name = "dsh-stack-guardian";
export const DEFAULTS = Object.freeze({
  cli: "stack-guardian",
  args: ["doctor", "--json", "--quiet"],
  budgetMs: 5000,
  injectOn: ["WARN", "FAIL"],
  maxChars: 400,
});

const RANK = { OK: 0, INFO: 1, WARN: 2, FAIL: 3 };

const rankOf = (level) => (typeof level === "string" && Object.prototype.hasOwnProperty.call(RANK, level) ? RANK[level] : 0);

export function worstLevel(findings) {
  if (!Array.isArray(findings) || findings.length === 0) return "OK";
  let worst = "OK";
  let worstRank = RANK.OK;
  for (const f of findings) {
    const level = f && typeof f === "object" ? f.level : undefined;
    const r = rankOf(level);
    if (r > worstRank) {
      worstRank = r;
      worst = level;
    }
  }
  return worst;
}

export function parseReport(stdout) {
  let parsed;
  try {
    parsed = JSON.parse(String(stdout || ""));
  } catch {
    return null;
  }
  if (parsed && typeof parsed === "object" && Array.isArray(parsed.findings)) return parsed;
  return null;
}

export function buildLine(report, opts = {}) {
  const injectOn = (opts && opts.injectOn) || DEFAULTS.injectOn;
  const maxChars = (opts && opts.maxChars) || DEFAULTS.maxChars;
  const findings = report && Array.isArray(report.findings) ? report.findings : [];
  const worst = worstLevel(findings);
  if (!injectOn.includes(worst)) return null;
  const bad = findings
    .map((f, i) => ({ f, i, r: rankOf(f && typeof f === "object" ? f.level : undefined) }))
    .filter((e) => e.r >= RANK.WARN)
    .sort((a, b) => (b.r - a.r) || (a.i - b.i))
    .map((e) => (e.f && typeof e.f === "object" ? e.f : {}));
  const items = bad.map((f) => `${f.node || "?"}: ${f.summary || f.code || "issue"}`);
  const text = `[stack-guardian] ${worst}: ${bad.length} issue(s) — ${items.join("; ")} (run "stack-guardian doctor" for details)`;
  if (text.length > maxChars) return text.slice(0, maxChars - 1) + "…";
  return text;
}

export function createGuard({ run, sleep, budgetMs = DEFAULTS.budgetMs, injectOn = DEFAULTS.injectOn, maxChars = DEFAULTS.maxChars, log = () => {} }) {
  const entries = new Map();
  return {
    start(key) {
      if (entries.has(key)) return false;
      entries.set(key, { done: false, promise: Promise.resolve().then(run) });
      return true;
    },
    async lineFor(key) {
      const entry = entries.get(key);
      if (!entry || entry.done) return null;
      entry.done = true;
      const timeoutMarker = {};
      const timer = Promise.resolve(sleep(budgetMs)).then(() => timeoutMarker);
      try {
        const out = await Promise.race([entry.promise, timer]);
        if (out === timeoutMarker) {
          log(`stack-guardian: doctor did not answer within ${budgetMs} ms; no warning added`);
          return null;
        }
        const report = parseReport(out && out.stdout);
        if (!report) {
          log("stack-guardian: doctor printed no JSON report; no warning added");
          return null;
        }
        return buildLine(report, { injectOn, maxChars });
      } catch (e) {
        log(`stack-guardian: doctor failed (${e && e.message}); no warning added`);
        return null;
      }
    },
  };
}

export function userMessage(text) {
  return { id: randomUUID(), role: "user", content: [{ type: "text", text }], source: { kind: "plugin", plugin: name } };
}

export function agentKey(agent) {
  return String((agent && (agent.id || (agent.session && agent.session.id))) || "default");
}

function spawnCli(opts) {
  return new Promise((resolve) => {
    execFile(opts.cli, opts.args, { timeout: opts.budgetMs + 1000, windowsHide: true }, (err, stdout) => {
      resolve({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout || "") });
    });
  });
}

export function apply(ctx, config = {}) {
  const opts = { ...DEFAULTS, ...(config || {}) };
  const guard = createGuard({
    run: () => spawnCli(opts),
    sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
    budgetMs: opts.budgetMs, injectOn: opts.injectOn, maxChars: opts.maxChars,
    log: (line) => ctx.logger && ctx.logger.info(line),
  });
  ctx.on("agent/session-start", ({ agent, source }) => {
    if (source === "startup") guard.start(agentKey(agent));
  });
  ctx.on("agent/pre-step", async ({ agent, signal }, next) => {
    const decision = await next();
    if (decision.kind === "reject" || (signal && signal.aborted)) return decision;
    const line = await guard.lineFor(agentKey(agent));
    if (!line) return decision;
    return { ...decision, messages: [...decision.messages, userMessage(line)] };
  }, { prepend: true });
}

export async function selftest() {
  const WARN = { findings: [{ level: "OK", node: "node1", summary: "fine" }, { level: "WARN", node: "atlas", summary: "model not loaded", code: "model.not_loaded" }] };
  const FAIL = { findings: [{ level: "WARN", node: "atlas", summary: "relayed" }, { level: "FAIL", node: "svc", summary: "Ollama does not answer" }] };
  const never = () => new Promise(() => {});
  const now = () => Promise.resolve();
  const guardWith = (run, sleep, logs) => createGuard({ run, sleep, budgetMs: 50, log: (l) => logs.push(l) });
  const checks = [
    ["defaults", () => DEFAULTS.budgetMs === 5000 && DEFAULTS.maxChars === 400 && DEFAULTS.injectOn.join(",") === "WARN,FAIL" && DEFAULTS.args.join(" ") === "doctor --json --quiet"],
    ["worst_level", () => worstLevel([]) === "OK" && worstLevel(WARN.findings) === "WARN" && worstLevel(FAIL.findings) === "FAIL" && worstLevel([{ level: "INFO" }]) === "INFO"],
    ["parse_report", () => parseReport(JSON.stringify(WARN)) !== null && parseReport("not json") === null && parseReport("{\"x\":1}") === null],
    ["ok_and_info_add_nothing", () => buildLine({ findings: [{ level: "OK" }] }) === null && buildLine({ findings: [{ level: "INFO", node: "a", summary: "b" }] }) === null],
    ["warn_line_exact", () => buildLine(WARN) === "[stack-guardian] WARN: 1 issue(s) — atlas: model not loaded (run \"stack-guardian doctor\" for details)"],
    ["fail_first", () => buildLine(FAIL) === "[stack-guardian] FAIL: 2 issue(s) — svc: Ollama does not answer; atlas: relayed (run \"stack-guardian doctor\" for details)"],
    ["max_chars", () => { const big = { findings: Array.from({ length: 40 }, (_, i) => ({ level: "WARN", node: "n" + i, summary: "x".repeat(30) })) }; const l = buildLine(big); return l.length === 400 && l.endsWith("…"); }],
    ["guard_warn_once", async () => { const logs = []; const g = guardWith(() => ({ code: 1, stdout: JSON.stringify(WARN) }), never, logs); g.start("a"); const first = await g.lineFor("a"); const second = await g.lineFor("a"); return first.startsWith("[stack-guardian] WARN") && second === null && logs.length === 0; }],
    ["guard_start_twice", () => { const g = guardWith(() => ({}), never, []); return g.start("a") === true && g.start("a") === false; }],
    ["guard_unknown_key_null", async () => (await guardWith(() => ({}), never, []).lineFor("zz")) === null],
    ["guard_timeout_one_log", async () => { const logs = []; const g = guardWith(never, now, logs); g.start("a"); return (await g.lineFor("a")) === null && logs.length === 1 && logs[0].includes("did not answer within 50 ms"); }],
    ["guard_bad_report_one_log", async () => { const logs = []; const g = guardWith(() => ({ code: 0, stdout: "Traceback" }), never, logs); g.start("a"); return (await g.lineFor("a")) === null && logs.length === 1; }],
    ["guard_run_throws_one_log", async () => { const logs = []; const g = guardWith(() => { throw new Error("ENOENT"); }, never, logs); g.start("a"); return (await g.lineFor("a")) === null && logs.length === 1 && logs[0].includes("ENOENT"); }],
    ["user_message_shape", () => { const m = userMessage("hi"); return m.role === "user" && m.content[0].text === "hi" && m.source.plugin === name && typeof m.id === "string"; }],
    ["agent_key", () => agentKey({ id: "x" }) === "x" && agentKey({ session: { id: "s" } }) === "s" && agentKey(null) === "default"],
  ];
  let passed = 0;
  for (const [n, fn] of checks) {
    let ok = false;
    try { ok = !!(await fn()); } catch (e) { console.log(`  FAIL ${n} -- raised ${e && e.name}: ${e && e.message}`); }
    if (ok) passed += 1;
    console.log((ok ? "  ok   " : "  FAIL ") + n);
  }
  console.log(`sg_dsh selftest: ${checks.length} checks, ${passed} passed, ${checks.length - passed} failed`);
  return { checks: checks.length, passed, failed: checks.length - passed };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href && process.argv.includes("--selftest")) {
  const r = await selftest();
  process.exit(r.checks > 0 && r.failed === 0 ? 0 : 1);
}