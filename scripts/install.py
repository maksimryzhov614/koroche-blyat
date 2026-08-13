#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import List, Optional, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.installer.hosts import resolve_config_dirs
from scripts.installer.manifest import load_manifest
from scripts.installer.model import InstallPlan, Options
from scripts.installer.plan import build_install_plan, build_uninstall_plan
from scripts.installer.sources import load_sources
from scripts.installer.journal import recover_pending
from scripts.installer.transaction import (
    REAL_FS, RollbackFailure, TransactionFailure, execute_transaction,
)


HOST_ORDER = ("prime", "codex", "claude")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        self.print_usage(sys.stderr)
        self.exit(2, "%s: error: invalid arguments\n" % self.prog)


def parse_args(argv: Sequence[str]) -> Options:
    parser = _Parser(prog="install.sh", allow_abbrev=False)
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--all", action="store_true")
    for host in HOST_ORDER:
        parser.add_argument("--%s" % host, action="store_true")
    namespace = parser.parse_args(list(argv))

    explicit = tuple(host for host in HOST_ORDER if getattr(namespace, host))
    if namespace.all and explicit:
        parser.error("--all cannot be combined with individual host flags")
    action = "uninstall" if namespace.uninstall else "install"
    if namespace.force and action != "uninstall":
        parser.error("--force is valid only with --uninstall")
    requested = HOST_ORDER if namespace.all or not explicit else explicit
    return Options(
        action=action,
        requested_hosts=requested,
        dry_run=namespace.dry_run,
        force=namespace.force,
        all=namespace.all or not explicit,
    )


def _dry_run_document(plan: InstallPlan) -> dict:
    return {
        "action": plan.action,
        "requested_hosts": list(plan.requested_hosts),
        "effective_hosts": list(plan.effective_hosts),
        "release": plan.release,
        "operations": [
            {"id": item.id, "kind": item.kind, "path": item.path, "change": item.change}
            for item in plan.operations
        ],
        "manual_actions": list(plan.manual_actions),
    }


def _build(options: Options) -> InstallPlan:
    repo_root = Path(__file__).resolve().parents[1]
    paths = resolve_config_dirs(os.environ)
    manifest_path = paths["state"] / "manifest.json"
    manifest = load_manifest(manifest_path, paths["home"])
    sources = load_sources(repo_root)
    if options.action == "uninstall":
        return build_uninstall_plan(options, paths, sources, manifest)
    return build_install_plan(options, paths, sources, manifest)


def _redacted_error(error: BaseException) -> str:
    text = str(error)
    replacements = []
    for name in (
        "PRIME_AGENT_CODING_AGENT_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
        "XDG_STATE_HOME", "HOME",
    ):
        value = os.environ.get(name)
        if value:
            replacements.append((value, "$" + name))
    for value, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(value, label)
    return text.replace("\r", " ").replace("\n", " ")



def _rollback_diagnostic(error: RollbackFailure) -> str:
    paths = [
        value for value in (error.journal_path, error.backup_path)
        if value is not None
    ]
    suffix = ""
    if paths:
        suffix = "; evidence: " + ", ".join(paths)
    return _redacted_error(Exception(str(error) + suffix))

def main(argv: Optional[Sequence[str]] = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    if options.dry_run:
        try:
            plan = _build(options)
        except (OSError, ValueError) as error:
            sys.stderr.write(
                "koroche-blyat: preflight failed: %s\n" % _redacted_error(error)
            )
            return 2
        sys.stdout.write(json.dumps(
            _dry_run_document(plan), ensure_ascii=False, separators=(",", ":")
        ) + "\n")
        return 0

    lock = None
    try:
        try:
            paths = resolve_config_dirs(os.environ)
            lock = REAL_FS.open_lock(paths["home"])
            recover_pending(paths["state"], fs=REAL_FS, home=paths["home"])
            plan = _build(options)
        except (OSError, ValueError, RollbackFailure) as error:
            sys.stderr.write(
                "koroche-blyat: recovery or preflight failed: %s\n"
                % (
                    _rollback_diagnostic(error)
                    if isinstance(error, RollbackFailure)
                    else _redacted_error(error)
                )
            )
            return 3 if isinstance(error, RollbackFailure) else 2
        try:
            execute_transaction(plan, lock=lock)
        except RollbackFailure as error:
            sys.stderr.write(
                "koroche-blyat: rollback incomplete; recovery evidence retained: %s\n"
                % _rollback_diagnostic(error)
            )
            return 3
        except TransactionFailure as error:
            label = "preflight failed" if error.preflight else "transaction failed and rolled back"
            sys.stderr.write(
                "koroche-blyat: %s: %s\n" % (label, _redacted_error(error))
            )
            return 2 if error.preflight else 1
        for action in plan.manual_actions:
            sys.stderr.write("koroche-blyat: manual action: %s\n" % action)
        return 0
    finally:
        if lock is not None:
            REAL_FS.close_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
