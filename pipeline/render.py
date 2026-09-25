"""Render the engine's plugin template with the owner's name and generated page (spec §6).

`install(cfg)` writes a filled-in copy of `plugins/brainprint/` to `<brain_dir>/plugin/`;
`install.sh`, `init` and `refresh` call it. The Claude Code hook and the Pi extension read
`<brain_dir>/plugin/advisory.md` at runtime.
"""
import json
import shutil
from pathlib import Path

from pipeline import config, mine, sessions

TEMPLATE = Path(__file__).resolve().parent.parent / "plugins" / "brainprint"
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
MARKETPLACE = "brainprint-local"  # `claude|codex plugin marketplace add <brain_dir>` registers it


def possessive(name):
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def fill(text, owner, page=""):
    return text.replace("{{owner's}}", possessive(owner)).replace("{{owner}}", owner) \
               .replace("{{page}}", page)


def fill_json(value, owner):
    if isinstance(value, str):
        return fill(value, owner)
    if isinstance(value, dict):
        return {k: fill_json(v, owner) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_json(v, owner) for v in value]
    return value


def plugin_dir(cfg):
    return cfg["brain_dir"] / "plugin"


def install(cfg):
    """Write the rendered plugin to `<brain_dir>/plugin/` and return that path."""
    owner = cfg["owner_name"]
    page = mine.page_text(cfg["brain_dir"] / "brain")
    out = plugin_dir(cfg)
    tmp = out.with_name("plugin.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(TEMPLATE, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    (tmp / "advisory.md").write_text(fill((TEMPLATE / "advisory.md").read_text(), owner, page))
    for rel in MANIFESTS:
        manifest = fill_json(json.loads((TEMPLATE / rel).read_text()), owner)
        sessions.write_json(tmp / rel, manifest, ensure_ascii=False)
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    write_marketplaces(cfg["brain_dir"], owner)
    return out


def write_marketplaces(root, owner):
    """Local marketplaces beside the plugin, so Claude Code and Codex can install it by name."""
    sessions.write_json(root / ".claude-plugin" / "marketplace.json", {
        "name": MARKETPLACE, "owner": {"name": owner},
        "plugins": [{"name": "brainprint", "description": f"{possessive(owner)} brain advisory",
                     "source": "./plugin"}]}, ensure_ascii=False)
    sessions.write_json(root / ".agents" / "plugins" / "marketplace.json", {
        "name": MARKETPLACE, "interface": {"displayName": f"{possessive(owner)} brainprint"},
        "plugins": [{"name": "brainprint", "source": {"source": "local", "path": "./plugin"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                     "category": "Productivity"}]}, ensure_ascii=False)


if __name__ == "__main__":
    print(install(config.load()))
