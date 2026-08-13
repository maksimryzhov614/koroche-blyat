from __future__ import annotations

from pathlib import Path
from typing import Mapping, Tuple
import unicodedata


def _absolute_home(env: Mapping[str, str]) -> Path:
    value = env.get("HOME")
    if not value:
        raise ValueError("HOME is required")
    raw = Path(value)
    if not raw.is_absolute():
        raise ValueError("HOME must be absolute")
    if ".." in raw.parts:
        raise ValueError("HOME must be canonical")
    if raw.exists() and (raw.is_symlink() or not raw.is_dir()):
        raise ValueError("HOME must be a regular directory")
    return raw.resolve(strict=False)


def _configured(env: Mapping[str, str], name: str, default: Path, home: Path) -> Path:
    value = env.get(name)
    if not value:
        return default
    if value == "~":
        return home
    if value.startswith("~/"):
        raw = home / value[2:]
    else:
        raw = Path(value)
        if not raw.is_absolute():
            raise ValueError("%s must be absolute or HOME-relative" % name)
    if ".." in raw.parts:
        raise ValueError("%s must be canonical" % name)
    return raw


def _inside_home(path: Path, home: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(home)
    except ValueError as error:
        raise ValueError("%s config root must be inside HOME" % label) from error
    current = path
    if current.is_symlink():
        raise ValueError("%s config root must not use symlinks" % label)
    while current.resolve(strict=False) != home:
        if current.is_symlink():
            raise ValueError("%s config root must not use symlinks" % label)
        if current.exists() and not current.is_dir():
            raise ValueError("%s config root ancestor must be a directory" % label)
        parent = current.parent
        if parent == current:
            raise ValueError("%s config root must be inside HOME" % label)
        current = parent
    return resolved

def _component_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _path_key(path: Path, home: Path) -> Tuple[str, ...]:
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise ValueError("managed path must be inside HOME") from error
    return tuple(_component_key(part) for part in relative.parts)


def _is_at_or_below(path: Path, root: Path, home: Path) -> bool:
    path_key = _path_key(path, home)
    root_key = _path_key(root, home)
    return path_key[:len(root_key)] == root_key


def _reject_managed_tree_overlaps(result: Mapping[str, Path]) -> None:
    home = result["home"]
    skill_relatives = (
        "LICENSE.txt", "NOTICE.md", "SKILL.md",
        "licenses/caveman-MIT.txt", "licenses/pohuy-MIT.txt",
        "references/compression.md", "references/ontologia.md",
        "references/sceny.md", "references/slovar.md",
    )
    shared = result["home"] / ".agents" / "skills" / "koroche-blyat"
    prime = result["prime"] / "extensions" / "koroche-blyat"
    codex_hooks = result["codex"] / "hooks" / "koroche-blyat"
    claude_skill = result["claude"] / "skills" / "koroche-blyat"
    claude_hooks = result["claude"] / "hooks" / "koroche-blyat"
    package_trees = {
        "shared": shared,
        "prime": prime,
        "codex": codex_hooks,
        "claude-skill": claude_skill,
        "claude-hooks": claude_hooks,
    }
    tree_items = list(package_trees.items())
    for index, (left_name, left) in enumerate(tree_items):
        for right_name, right in tree_items[index + 1:]:
            if (
                _path_key(left, home) == _path_key(right, home)
                or _is_at_or_below(left, right, home)
                or _is_at_or_below(right, left, home)
            ):
                raise ValueError(
                    "managed package trees overlap: %s and %s" % (
                        left_name, right_name,
                    )
                )
    files = []
    files.extend(("shared/" + relative, shared / relative) for relative in skill_relatives)
    files.extend(("claude-skill/" + relative, claude_skill / relative) for relative in skill_relatives)
    files.extend((
        ("prime/index", prime / "index.ts"),
        ("prime/policy", prime / "always-on.md"),
        ("prime/reminder", prime / "reminder.txt"),
        ("codex/agents", result["codex"] / "AGENTS.md"),
        ("codex/override", result["codex"] / "AGENTS.override.md"),
        ("codex/hooks-config", result["codex"] / "hooks.json"),
        ("codex/hook-script", codex_hooks / "user-prompt-reminder.sh"),
        ("codex/hook-reminder", codex_hooks / "reminder.txt"),
        ("claude/output-style", result["claude"] / "output-styles" / "koroche-blyat.md"),
        ("claude/settings", result["claude"] / "settings.json"),
        ("claude/hook-script", claude_hooks / "user-prompt-reminder.sh"),
        ("claude/hook-reminder", claude_hooks / "reminder.txt"),
        ("state/manifest", result["state"] / "manifest.json"),
        ("state/scalar-baseline", result["state"] / "baselines" / "claude-output-style-setting.token"),
    ))
    for index, (left_name, left) in enumerate(files):
        for right_name, right in files[index + 1:]:
            if (
                _path_key(left, home) == _path_key(right, home)
                or _is_at_or_below(left, right, home)
                or _is_at_or_below(right, left, home)
            ):
                raise ValueError(
                    "managed file targets collide: %s and %s" % (
                        left_name, right_name,
                    )
                )
    for root_name in ("prime", "codex", "claude", "state"):
        root = result[root_name]
        for tree_name, tree in package_trees.items():
            own_tree = tree_name == root_name or (
                root_name == "claude" and tree_name.startswith("claude-")
            )
            if not own_tree and _is_at_or_below(root, tree, home):
                raise ValueError("config root overlaps a managed package tree")
        for file_name, target in files:
            if (
                _path_key(root, home) == _path_key(target, home)
                or _is_at_or_below(root, target, home)
            ):
                raise ValueError(
                    "config root overlaps managed file target: %s" % file_name
                )


def resolve_config_dirs(env: Mapping[str, str]) -> Mapping[str, Path]:
    home = _absolute_home(env)
    prime = _configured(
        env, "PRIME_AGENT_CODING_AGENT_DIR", home / ".prime" / "agent", home
    )
    codex = _configured(env, "CODEX_HOME", home / ".codex", home)
    claude = _configured(env, "CLAUDE_CONFIG_DIR", home / ".claude", home)
    state_base = _configured(
        env, "XDG_STATE_HOME", home / ".local" / "state", home
    )
    result = {
        "home": home,
        "prime": _inside_home(prime, home, "Prime"),
        "codex": _inside_home(codex, home, "Codex"),
        "claude": _inside_home(claude, home, "Claude"),
        "state": _inside_home(state_base / "koroche-blyat", home, "state"),
    }
    config_values = (
        result["prime"], result["codex"], result["claude"], result["state"]
    )
    config_keys = {_path_key(path, home) for path in config_values}
    if len(config_keys) != len(config_values):
        raise ValueError("config roots must be unique")
    _reject_managed_tree_overlaps(result)
    return result
