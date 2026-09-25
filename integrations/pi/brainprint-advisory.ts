import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";


type Config = { brain_dir: string; scope?: string[] };


function advisoryEnabled(): boolean {
  return !["0", "false", "off", "no"].includes(
    (process.env.BRAINPRINT_ADVISORY ?? "1").toLowerCase(),
  );
}


function expand(path: string): string {
  return path === "~" || path.startsWith("~/") ? join(homedir(), path.slice(1)) : path;
}


/** Same resolution as pipeline/config.py: BRAINPRINT_CONFIG, BRAINPRINT_HOME, ~/.brainprint. */
function configPath(): string {
  if (process.env.BRAINPRINT_CONFIG) return expand(process.env.BRAINPRINT_CONFIG);
  const home = process.env.BRAINPRINT_HOME
    ? expand(process.env.BRAINPRINT_HOME)
    : join(homedir(), ".brainprint");
  return join(home, "brainprint.config.json");
}


/** No `scope` in the config means everywhere. Scope is a path boundary, not a prefix. */
function inScope(config: Config, cwd: string | undefined): boolean {
  if (!config.scope) return true;
  if (!cwd) return false;
  return config.scope
    .map((dir) => resolve(expand(dir)))
    .some((dir) => cwd === dir || cwd.startsWith(`${dir}/`));
}


/** The rendered advisory, or undefined when there is no brain or it is not rendered yet. */
function advisoryFor(cwd: string | undefined): string | undefined {
  const path = configPath();
  if (!existsSync(path)) return undefined;
  const config = JSON.parse(readFileSync(path, "utf8")) as Config;
  if (!inScope(config, cwd)) return undefined;
  const advisory = join(resolve(dirname(path), expand(config.brain_dir)), "plugin", "advisory.md");
  if (!existsSync(advisory)) return undefined;
  return readFileSync(advisory, "utf8").trim();
}


export default function brainprintAdvisory(pi: ExtensionAPI) {
  pi.on("before_agent_start", async (event, ctx) => {
    if (!advisoryEnabled()) return undefined;
    const advisory = advisoryFor(ctx.cwd);
    if (!advisory) return undefined;
    return { systemPrompt: `${event.systemPrompt}\n\n${advisory}` };
  });
}
