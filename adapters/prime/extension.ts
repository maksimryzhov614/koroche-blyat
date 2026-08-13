import { closeSync, constants, existsSync, fstatSync, openSync, readFileSync, realpathSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export interface LoadedPolicy {
  text: string;
  mode: "full" | "reminder" | "none";
  marker: string;
}

export interface AdapterOptions {
  assetsDir?: string;
  skillRoot?: string;
}

const HASH_HEADER = /^canonical-sha256: ([0-9a-f]{64})\n\n/;
const FULL_FALLBACK = "koroche-blyat: full policy not found, using reminder";
const NO_ASSETS = "koroche-blyat: no policy assets found, not injecting";
// Generated policy assets are about 10 KiB. This cap keeps corrupt local files from
// blocking Prime during synchronous registration while leaving ample release headroom.
const MAX_ASSET_BYTES = 256 * 1024;

function safeRead(path: string): string | undefined {
  let descriptor: number | undefined;
  try {
    const noFollow = typeof constants.O_NOFOLLOW === "number" ? constants.O_NOFOLLOW : 0;
    descriptor = openSync(path, constants.O_RDONLY | noFollow);
    const metadata = fstatSync(descriptor);
    if (!metadata.isFile() || metadata.size > MAX_ASSET_BYTES) return undefined;
    const bytes = readFileSync(descriptor);
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
    if (text.startsWith("\uFEFF") || text.includes("\r") || !text.endsWith("\n")) {
      return undefined;
    }
    return text;
  } catch {
    return undefined;
  } finally {
    if (descriptor !== undefined) {
      try { closeSync(descriptor); } catch { /* already closed or host teardown */ }
    }
  }
}

function parseFull(text: string | undefined): LoadedPolicy | undefined {
  if (text === undefined) return undefined;
  const match = HASH_HEADER.exec(text);
  if (match === null) return undefined;
  const header = `canonical-sha256: ${match[1]}`;
  if (text.split("canonical-sha256:").length - 1 !== 1) return undefined;
  if (text.slice(match[0].length).trim().length === 0) return undefined;
  return { text, mode: "full", marker: header };
}

function parseReminder(text: string | undefined): LoadedPolicy | undefined {
  if (text === undefined) return undefined;
  const match = HASH_HEADER.exec(text);
  if (match === null) return undefined;
  const payload = text.slice(match[0].length, -1);
  if (payload.length === 0 || payload !== payload.trim() || payload.includes("\n")) return undefined;
  return { text: payload, mode: "reminder", marker: payload };
}

export function loadPolicy(options: AdapterOptions = {}): LoadedPolicy {
  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const assetsDir = resolve(options.assetsDir ?? extensionDir);
  const full = parseFull(safeRead(join(assetsDir, "always-on.md")));
  if (full !== undefined) return full;

  const reminder = parseReminder(safeRead(join(assetsDir, "reminder.txt")));
  if (reminder !== undefined) {
    console.error(FULL_FALLBACK);
    return reminder;
  }

  console.error(NO_ASSETS);
  return { text: "", mode: "none", marker: "" };
}

export function appendPolicy(systemPrompt: string, loaded: LoadedPolicy): string {
  if (loaded.mode === "none" || loaded.text.length === 0) return systemPrompt;
  const hasMarkerLine = loaded.marker.length > 0 && systemPrompt
    .split("\n")
    .some((line) => line.replace(/\r$/, "") === loaded.marker);
  if (hasMarkerLine) return systemPrompt;
  return `${systemPrompt}\n\n${loaded.text}`;
}

function existingDirectory(path: string): string | undefined {
  try {
    if (!existsSync(path) || !statSync(path).isDirectory()) return undefined;
    return resolve(path);
  } catch {
    return undefined;
  }
}

function canonicalPath(path: string): string {
  try { return realpathSync(path); }
  catch { return resolve(path); }
}

function isInside(path: string, root: string): boolean {
  const child = canonicalPath(path);
  const parent = canonicalPath(root);
  const suffix = relative(parent, child);
  return suffix === "" || (!suffix.startsWith("..") && !isAbsolute(suffix));
}

function agentDir(): string {
  const configured = process.env.PRIME_AGENT_CODING_AGENT_DIR;
  if (!configured) return join(homedir(), ".prime", "agent");
  if (configured === "~") return homedir();
  if (configured.startsWith("~/")) return join(homedir(), configured.slice(2));
  return resolve(configured);
}

function repoSkillRoot(extensionDir: string): string | undefined {
  return existingDirectory(resolve(extensionDir, "../../skills/koroche-blyat"));
}

function shouldContributeSkill(skillRoot: string): boolean {
  const userRoots = [join(agentDir(), "skills"), join(homedir(), ".agents", "skills")];
  return !userRoots.some((root) => isInside(skillRoot, root));
}

function isProjectDefaultSkill(skillRoot: string, cwd: string): boolean {
  const start = resolve(cwd);
  if (isInside(skillRoot, join(start, ".prime", "agent", "skills"))) return true;
  let current = start;
  while (true) {
    if (isInside(skillRoot, join(current, ".agents", "skills"))) return true;
    if (existsSync(join(current, ".git"))) return false;
    const parent = dirname(current);
    if (parent === current) return false;
    current = parent;
  }
}

export function registerKorocheBlyat(
  pi: ExtensionAPI,
  options: AdapterOptions = {},
): void {
  const loaded = loadPolicy(options);

  pi.on("before_agent_start", (event) => {
    const systemPrompt = appendPolicy(event.systemPrompt, loaded);
    if (systemPrompt === event.systemPrompt) return;
    return { systemPrompt };
  });

  const extensionDir = dirname(fileURLToPath(import.meta.url));
  const skillRoot = existingDirectory(options.skillRoot ?? "") ??
    (options.skillRoot === undefined ? repoSkillRoot(extensionDir) : undefined);
  if (skillRoot !== undefined && shouldContributeSkill(skillRoot)) {
    pi.on("resources_discover", (event) => {
      if (isProjectDefaultSkill(skillRoot, event.cwd)) return;
      return { skillPaths: [skillRoot] };
    });
  }
}

export default function korocheBlyat(pi: ExtensionAPI): void {
  registerKorocheBlyat(pi);
}
