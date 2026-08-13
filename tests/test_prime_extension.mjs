import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, copyFile, readFile, realpath, rm, symlink, writeFile, unlink } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const FIXTURES = join(ROOT, "tests", "fixtures", "prime");
const GENERATED = join(ROOT, "adapters", "generated");
const EXTENSION = join(ROOT, "adapters", "prime", "extension.ts");
const POLICY_HASH = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const POLICY = `canonical-sha256: ${POLICY_HASH}\n\nPOLICY-LINE\n`;
const REMINDER = "Контракт fixture остаётся активен.";

const mod = await import(pathToFileURL(EXTENSION).href);

class FakeExtensionAPI {
  constructor() { this.handlers = new Map(); }
  on(event, handler) {
    const handlers = this.handlers.get(event) ?? [];
    handlers.push(handler);
    this.handlers.set(event, handlers);
  }
  one(event) {
    const handlers = this.handlers.get(event) ?? [];
    assert.equal(handlers.length, 1, `expected one ${event} handler`);
    return handlers[0];
  }
}

async function temporaryDir(prefix = "koroche-prime-") {
  return await mkdtemp(join(tmpdir(), prefix));
}

async function primePackageRoot(t) {
  try {
    execFileSync("prime-agent", ["--version"], { stdio: "pipe" });
  } catch (error) {
    if (error?.code === "ENOENT") {
      t.skip("Prime Agent executable is not installed");
      return undefined;
    }
    throw error;
  }

  const candidates = [];
  if (process.env.PRIME_AGENT_PACKAGE_ROOT) {
    candidates.push(resolve(process.env.PRIME_AGENT_PACKAGE_ROOT));
  }
  try {
    candidates.push(join(execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim(), "prime-agent"));
  } catch { /* another package manager may own the executable */ }
  try {
    let current = dirname(await realpath(execFileSync("which", ["prime-agent"], { encoding: "utf8" }).trim()));
    while (true) {
      candidates.push(current);
      const parent = dirname(current);
      if (parent === current) break;
      current = parent;
    }
  } catch { /* candidates above may still identify the package */ }

  for (const candidate of candidates) {
    try {
      const metadata = JSON.parse(await readFile(join(candidate, "package.json"), "utf8"));
      if (metadata.name === "prime-agent") return candidate;
    } catch { /* not the package root */ }
  }
  assert.fail("Prime Agent is installed but its package root could not be located");
}

async function captureDiagnostics(run) {
  const lines = [];
  const original = console.error;
  console.error = (...args) => lines.push(args.join(" "));
  try { return { value: await run(), lines }; }
  finally { console.error = original; }
}

function event(systemPrompt, cwd = ROOT) {
  return { type: "before_agent_start", prompt: "test", systemPrompt,
    systemPromptOptions: { cwd } };
}

test("registers before_agent_start without output post-processing", async () => {
  const pi = new FakeExtensionAPI();
  await captureDiagnostics(() => mod.default(pi));
  assert.equal(pi.handlers.get("before_agent_start")?.length, 1);
  assert.deepEqual([...pi.handlers.keys()].sort(), ["before_agent_start", "resources_discover"]);
});

test("injects the full generated policy on every prompt exactly once", async () => {
  const assets = await temporaryDir();
  await writeFile(join(assets, "always-on.md"), POLICY);
  const pi = new FakeExtensionAPI();
  mod.registerKorocheBlyat(pi, { assetsDir: assets });
  const handler = pi.one("before_agent_start");
  const first = await handler(event("BASE"));
  assert.deepEqual(first, { systemPrompt: `BASE\n\n${POLICY}` });
  assert.equal(first.systemPrompt.split(POLICY_HASH).length - 1, 1);
  assert.equal(await handler(event(first.systemPrompt)), undefined);
  const nextPrompt = await handler(event("NEXT"));
  assert.deepEqual(nextPrompt, { systemPrompt: `NEXT\n\n${POLICY}` });
  await rm(assets, { recursive: true, force: true });
});

test("preserves the original system prompt as an exact prefix", () => {
  const original = "  A\r\nB\n\0TAIL  ";
  const loaded = { text: POLICY, mode: "full", marker: `canonical-sha256: ${POLICY_HASH}` };
  const result = mod.appendPolicy(original, loaded);
  assert.equal(result.slice(0, original.length), original);
  assert.equal(result, original + "\n\n" + POLICY);
});

test("reads assets once at registration, not once per turn", async () => {
  const assets = await temporaryDir();
  await writeFile(join(assets, "always-on.md"), POLICY);
  const pi = new FakeExtensionAPI();
  mod.registerKorocheBlyat(pi, { assetsDir: assets });
  await unlink(join(assets, "always-on.md"));
  const handler = pi.one("before_agent_start");
  assert.equal((await handler(event("ONE"))).systemPrompt, `ONE\n\n${POLICY}`);
  assert.equal((await handler(event("TWO"))).systemPrompt, `TWO\n\n${POLICY}`);
  await rm(assets, { recursive: true, force: true });
});

test("falls back to the canonical reminder and one diagnostic", async () => {
  const assets = await temporaryDir();
  await copyFile(join(FIXTURES, "reminder.txt"), join(assets, "reminder.txt"));
  const pi = new FakeExtensionAPI();
  const captured = await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: assets }));
  assert.deepEqual(captured.lines, ["koroche-blyat: full policy not found, using reminder"]);
  const handler = pi.one("before_agent_start");
  const first = await handler(event("BASE"));
  assert.deepEqual(first, { systemPrompt: `BASE\n\n${REMINDER}` });
  assert.equal(await handler(event(first.systemPrompt)), undefined);
  await rm(assets, { recursive: true, force: true });
});

test("missing full and reminder assets leave the host alive", async () => {
  const assets = await temporaryDir();
  const pi = new FakeExtensionAPI();
  const captured = await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: assets }));
  assert.deepEqual(captured.lines, ["koroche-blyat: no policy assets found, not injecting"]);
  assert.equal(await pi.one("before_agent_start")(event("UNCHANGED")), undefined);
  await rm(assets, { recursive: true, force: true });
});

test("resolves extension paths containing spaces from import.meta.url", async () => {
  const base = await temporaryDir("koroche prime spaces ");
  const copiedDir = join(base, "adapter with spaces");
  await mkdir(copiedDir, { recursive: true });
  await copyFile(EXTENSION, join(copiedDir, "extension.ts"));
  await copyFile(join(FIXTURES, "policy.md"), join(copiedDir, "always-on.md"));
  const copied = await import(pathToFileURL(join(copiedDir, "extension.ts")).href + `?v=${Date.now()}`);
  assert.deepEqual(copied.loadPolicy(), {
    text: POLICY, mode: "full", marker: `canonical-sha256: ${POLICY_HASH}`,
  });
  await rm(base, { recursive: true, force: true });
});

test("does not duplicate an auto-discovered skill root", async () => {
  const home = await temporaryDir();
  const agentDir = join(home, ".prime", "agent");
  const skillRoot = join(agentDir, "skills", "koroche-blyat");
  await mkdir(skillRoot, { recursive: true });
  const previous = process.env.PRIME_AGENT_CODING_AGENT_DIR;
  process.env.PRIME_AGENT_CODING_AGENT_DIR = agentDir;
  try {
    const pi = new FakeExtensionAPI();
    await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES, skillRoot }));
    assert.equal(pi.handlers.has("resources_discover"), false);
  } finally {
    if (previous === undefined) delete process.env.PRIME_AGENT_CODING_AGENT_DIR;
    else process.env.PRIME_AGENT_CODING_AGENT_DIR = previous;
    await rm(home, { recursive: true, force: true });
  }
});

test("repo execution contributes one outside-default skill root", async () => {
  const base = await temporaryDir();
  const skillRoot = join(base, "repo", "skills", "koroche-blyat");
  await mkdir(skillRoot, { recursive: true });
  const pi = new FakeExtensionAPI();
  await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES, skillRoot }));
  const result = await pi.one("resources_discover")({ type: "resources_discover", cwd: join(base, "repo"), reason: "startup" });
  assert.deepEqual(result, { skillPaths: [resolve(skillRoot)] });
  await rm(base, { recursive: true, force: true });
});

test("invalid full asset safely falls back and invalid reminder safely disables", async () => {
  const assets = await temporaryDir();
  await writeFile(join(assets, "always-on.md"), "canonical-sha256: short\n\nBAD\n");
  await copyFile(join(FIXTURES, "reminder.txt"), join(assets, "reminder.txt"));
  let captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "reminder");
  assert.equal(captured.lines.length, 1);
  await writeFile(join(assets, "reminder.txt"), "not valid\n");
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.deepEqual(captured.value, { text: "", mode: "none", marker: "" });
  assert.deepEqual(captured.lines, ["koroche-blyat: no policy assets found, not injecting"]);
  await rm(assets, { recursive: true, force: true });
});

test("no-model raw prompt smoke injects the real canonical hash once", async () => {
  const loaded = mod.loadPolicy({ assetsDir: GENERATED });
  assert.equal(loaded.mode, "full");
  const original = "PRIME-BASE\r\n  exact-prefix\0";
  const finalPrompt = mod.appendPolicy(original, loaded);
  assert.equal(finalPrompt.slice(0, original.length), original);
  assert.equal(finalPrompt.split(loaded.marker).length - 1, 1);
  assert.equal(mod.appendPolicy(finalPrompt, loaded), finalPrompt);
});


test("default repo discovery contributes the exact canonical skill directory", async () => {
  const pi = new FakeExtensionAPI();
  await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES }));
  const result = await pi.one("resources_discover")({
    type: "resources_discover", cwd: ROOT, reason: "startup",
  });
  assert.deepEqual(result, { skillPaths: [join(ROOT, "skills", "koroche-blyat")] });
});

test("symlink to an auto-discovered user skill root is not contributed", async () => {
  const home = await temporaryDir();
  const agentDir = join(home, ".prime", "agent");
  const realSkill = join(agentDir, "skills", "koroche-blyat");
  const outside = join(home, "outside-link");
  await mkdir(realSkill, { recursive: true });
  await symlink(realSkill, outside, "dir");
  const previous = process.env.PRIME_AGENT_CODING_AGENT_DIR;
  process.env.PRIME_AGENT_CODING_AGENT_DIR = agentDir;
  try {
    const pi = new FakeExtensionAPI();
    await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES, skillRoot: outside }));
    assert.equal(pi.handlers.has("resources_discover"), false);
  } finally {
    if (previous === undefined) delete process.env.PRIME_AGENT_CODING_AGENT_DIR;
    else process.env.PRIME_AGENT_CODING_AGENT_DIR = previous;
    await rm(home, { recursive: true, force: true });
  }
});

test("project-default skill root does not register resource discovery", async () => {
  const base = await temporaryDir();
  const project = join(base, "project");
  const skillRoot = join(project, ".prime", "agent", "skills", "koroche-blyat");
  await mkdir(skillRoot, { recursive: true });
  const pi = new FakeExtensionAPI();
  await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES, skillRoot }));
  const result = await pi.one("resources_discover")({
    type: "resources_discover", cwd: project, reason: "startup",
  });
  assert.equal(result, undefined);
  await rm(base, { recursive: true, force: true });
});


test("Prime temp-config no-model raw prompt smoke preserves prefix and injects hash once", async (t) => {
  const packageRoot = await primePackageRoot(t);
  if (packageRoot === undefined) return;
  await readFile(join(packageRoot, "dist", "core", "extensions", "loader.js"));
  await readFile(join(packageRoot, "dist", "core", "extensions", "runner.js"));

  const tempAgentDir = await temporaryDir("koroche prime config ");
  const adapterDir = join(tempAgentDir, "extensions", "koroche-blyat");
  const capturePath = join(tempAgentDir, "captured-system-prompt.bin");
  const captureExtension = join(tempAgentDir, "zz-capture.ts");
  await mkdir(adapterDir, { recursive: true });
  await copyFile(EXTENSION, join(adapterDir, "index.ts"));
  await copyFile(join(GENERATED, "always-on.md"), join(adapterDir, "always-on.md"));
  await copyFile(join(GENERATED, "reminder.txt"), join(adapterDir, "reminder.txt"));
  await writeFile(captureExtension, `
    import { writeFileSync } from "node:fs";
    export default function capture(pi) {
      pi.on("before_agent_start", (event) => {
        writeFileSync(${JSON.stringify(capturePath)}, Buffer.from(event.systemPrompt, "utf8"));
      });
    }
  `);

  const loaderUrl = pathToFileURL(join(packageRoot, "dist", "core", "extensions", "loader.js")).href;
  const runnerUrl = pathToFileURL(join(packageRoot, "dist", "core", "extensions", "runner.js")).href;
  const { discoverAndLoadExtensions } = await import(loaderUrl);
  const { ExtensionRunner } = await import(runnerUrl);
  const loaded = await discoverAndLoadExtensions([captureExtension], ROOT, tempAgentDir);
  assert.deepEqual(loaded.errors, []);
  assert.equal(loaded.extensions.length, 2);
  const runner = new ExtensionRunner(loaded.extensions, loaded.runtime, ROOT, {}, {});
  const original = "PRIME-ACTUAL-BASE\r\n  exact-prefix\0";
  const result = await runner.emitBeforeAgentStart("probe", undefined, original, { cwd: ROOT });
  assert.ok(result?.systemPrompt);
  const captured = await readFile(capturePath, "utf8");
  assert.equal(captured, result.systemPrompt);
  assert.equal(captured.slice(0, original.length), original);
  const hash = mod.loadPolicy({ assetsDir: GENERATED }).marker;
  assert.equal(captured.split(hash).length - 1, 1);
  await rm(tempAgentDir, { recursive: true, force: true });
});


test("invalid UTF-8 and duplicate full headers are corrupt, never injected", async () => {
  const assets = await temporaryDir();
  await writeFile(join(assets, "always-on.md"), Buffer.from([0xff, 0xfe, 0x0a]));
  let captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");
  await writeFile(join(assets, "always-on.md"), `JUNK\n${POLICY}`);
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");
  await writeFile(join(assets, "always-on.md"), POLICY + `canonical-sha256: ${POLICY_HASH}\n`);
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");
  await writeFile(join(assets, "always-on.md"), `canonical-sha256: ${POLICY_HASH}\n\n   \n`);
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");
  await rm(assets, { recursive: true, force: true });
});


test("does not duplicate the globally auto-discovered ~/.agents skill root", async () => {
  const home = await temporaryDir();
  const skillRoot = join(home, ".agents", "skills", "koroche-blyat");
  await mkdir(skillRoot, { recursive: true });
  const previous = process.env.HOME;
  process.env.HOME = home;
  try {
    const pi = new FakeExtensionAPI();
    await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES, skillRoot }));
    assert.equal(pi.handlers.has("resources_discover"), false);
  } finally {
    if (previous === undefined) delete process.env.HOME;
    else process.env.HOME = previous;
    await rm(home, { recursive: true, force: true });
  }
});

test("ancestor .agents skill root is not duplicated for project execution", async () => {
  const base = await temporaryDir();
  const project = join(base, "repo");
  const cwd = join(project, "nested", "work");
  const skillRoot = join(project, ".agents", "skills", "koroche-blyat");
  await mkdir(join(project, ".git"), { recursive: true });
  await mkdir(cwd, { recursive: true });
  await mkdir(skillRoot, { recursive: true });
  const pi = new FakeExtensionAPI();
  await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES, skillRoot }));
  const result = await pi.one("resources_discover")({
    type: "resources_discover", cwd, reason: "startup",
  });
  assert.equal(result, undefined);
  await rm(base, { recursive: true, force: true });
});


test("BOM assets, whitespace reminders, and mixed hash headers fail closed", async () => {
  const assets = await temporaryDir();
  const bom = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(POLICY)]);
  await writeFile(join(assets, "always-on.md"), bom);
  let captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");

  await writeFile(join(assets, "always-on.md"), `canonical-sha256: ${POLICY_HASH}\n\nX\ncanonical-sha256: ${"f".repeat(64)}\n`);
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");

  await unlink(join(assets, "always-on.md"));
  await writeFile(join(assets, "reminder.txt"), `canonical-sha256: ${POLICY_HASH}\n\n   \n`);
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");

  await writeFile(join(assets, "reminder.txt"), Buffer.concat([
    Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(`canonical-sha256: ${POLICY_HASH}\n\n${REMINDER}\n`),
  ]));
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");
  await rm(assets, { recursive: true, force: true });
});

test("marker embedded inside another line does not suppress injection", () => {
  const loaded = { text: POLICY, mode: "full", marker: `canonical-sha256: ${POLICY_HASH}` };
  const original = `prefix-${loaded.marker}-suffix`;
  assert.equal(mod.appendPolicy(original, loaded), `${original}\n\n${POLICY}`);
});

test("repo source layout intentionally requires staged sibling assets", async () => {
  const captured = await captureDiagnostics(() => mod.loadPolicy());
  assert.deepEqual(captured.value, { text: "", mode: "none", marker: "" });
  assert.deepEqual(captured.lines, ["koroche-blyat: no policy assets found, not injecting"]);
});




test("asset symlinks and oversized assets fail closed", async () => {
  const root = await temporaryDir();
  const assets = join(root, "assets");
  await mkdir(assets);
  const outside = join(root, "outside.md");
  await writeFile(outside, POLICY);
  await symlink(outside, join(assets, "always-on.md"));
  let captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");
  await unlink(join(assets, "always-on.md"));
  await writeFile(join(assets, "always-on.md"), `canonical-sha256: ${POLICY_HASH}\n\n${"X".repeat(300_000)}\n`);
  captured = await captureDiagnostics(() => mod.loadPolicy({ assetsDir: assets }));
  assert.equal(captured.value.mode, "none");
  await rm(root, { recursive: true, force: true });
});

test("agent dir handling matches Prime for empty and tilde env values", async () => {
  const home = await temporaryDir();
  const previousHome = process.env.HOME;
  const previousAgent = process.env.PRIME_AGENT_CODING_AGENT_DIR;
  process.env.HOME = home;
  try {
    for (const value of ["", "~/custom-agent"]) {
      process.env.PRIME_AGENT_CODING_AGENT_DIR = value;
      const base = value === "" ? join(home, ".prime", "agent") : join(home, "custom-agent");
      const skillRoot = join(base, "skills", "koroche-blyat");
      await mkdir(skillRoot, { recursive: true });
      const pi = new FakeExtensionAPI();
      await captureDiagnostics(() => mod.registerKorocheBlyat(pi, { assetsDir: FIXTURES, skillRoot }));
      assert.equal(pi.handlers.has("resources_discover"), false, `env=${value}`);
    }
  } finally {
    if (previousHome === undefined) delete process.env.HOME;
    else process.env.HOME = previousHome;
    if (previousAgent === undefined) delete process.env.PRIME_AGENT_CODING_AGENT_DIR;
    else process.env.PRIME_AGENT_CODING_AGENT_DIR = previousAgent;
    await rm(home, { recursive: true, force: true });
  }
});
