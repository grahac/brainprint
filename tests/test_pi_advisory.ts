/**
 * The Pi extension: advisory injection only, gated by the kill switch and the
 * optional `scope` list in brainprint.config.json.
 *
 * Run: node --test tests/test_pi_advisory.ts
 */
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

const EXTENSION = new URL("../integrations/pi/brainprint-advisory.ts", import.meta.url).href;
const ADVISORY = "Dana's brain recommends: <one concrete option>";

type Handler = (event: unknown, ctx: unknown) => Promise<unknown> | unknown;

let root: string;
let version = 0;

async function handlersFor(): Promise<Map<string, Handler>> {
  version += 1;
  const mod = await import(`${EXTENSION}?v=${version}`);
  const handlers = new Map<string, Handler>();
  mod.default({ on: (name: string, handler: Handler) => handlers.set(name, handler) });
  return handlers;
}

function writeBrain(extra: Record<string, unknown> = {}, rendered = true) {
  const home = join(root, "bp");
  mkdirSync(join(home, "plugin"), { recursive: true });
  writeFileSync(
    join(home, "brainprint.config.json"),
    JSON.stringify({ owner_name: "Dana", brain_dir: ".", ...extra }),
  );
  if (rendered) writeFileSync(join(home, "plugin", "advisory.md"), `${ADVISORY}\n`);
  process.env.BRAINPRINT_HOME = home;
}

async function inject(cwd: string) {
  const handlers = await handlersFor();
  return (await handlers.get("before_agent_start")!({ systemPrompt: "base prompt" }, { cwd })) as
    | { systemPrompt: string }
    | undefined;
}

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "brainprint-pi-"));
  process.env.HOME = root;
  delete process.env.BRAINPRINT_ADVISORY;
  delete process.env.BRAINPRINT_CONFIG;
  delete process.env.BRAINPRINT_HOME;
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

test("injects the rendered advisory after the base prompt", async () => {
  writeBrain();
  const result = await inject(join(root, "anywhere"));
  assert.equal(result?.systemPrompt, `base prompt\n\n${ADVISORY}`);
});

test("registers only before_agent_start", async () => {
  writeBrain();
  const handlers = await handlersFor();
  assert.deepEqual([...handlers.keys()], ["before_agent_start"]);
});

test("BRAINPRINT_ADVISORY=0 injects nothing", async () => {
  writeBrain();
  for (const off of ["0", "false", "off", "no"]) {
    process.env.BRAINPRINT_ADVISORY = off;
    assert.equal(await inject(join(root, "anywhere")), undefined);
  }
});

test("in scope injects, out of scope and prefix siblings do not", async () => {
  writeBrain({ scope: [join(root, "dev"), "~/work"] });
  assert.ok(await inject(join(root, "dev")));
  assert.ok(await inject(join(root, "dev", "proj")));
  assert.ok(await inject(join(root, "work", "x")));
  assert.equal(await inject(join(root, "dev_archived")), undefined);
  assert.equal(await inject(join(root, "elsewhere")), undefined);
});

test("BRAINPRINT_CONFIG wins over BRAINPRINT_HOME", async () => {
  writeBrain({ scope: [join(root, "nowhere")] });
  const other = join(root, "other.json");
  writeFileSync(other, JSON.stringify({ owner_name: "Dana", brain_dir: "bp" }));
  process.env.BRAINPRINT_CONFIG = other;
  assert.ok(await inject(join(root, "anywhere")));
});

test("no config or no rendered advisory injects nothing", async () => {
  assert.equal(await inject(root), undefined);
  writeBrain({}, false);
  assert.equal(await inject(root), undefined);
});
