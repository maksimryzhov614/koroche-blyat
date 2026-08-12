# Host Capability Contract

Verified floors: Prime Agent 0.7.1, Codex CLI 0.147.0, and Claude Code 2.1.197.

| Host | State | Activation | Limitations and required action |
|---|---|---|---|
| Prime | `SUPPORTED` | Global `before_agent_start` extension injects the full generated policy | RLM-child inheritance, resume, reload, and compaction require black-box evidence. |
| Prime | `UNSUPPORTED` | `--no-extensions` or `--no-context-files` prevents the required path | Remove the explicit bypass or uninstall; never claim activation. |
| Codex | `SUPPORTED` | Full policy in the active global file: non-empty `$CODEX_HOME/AGENTS.override.md`, otherwise `$CODEX_HOME/AGENTS.md` | The reminder-only `UserPromptSubmit` command hook requires manual trust through `/hooks`; cold start must not depend on that trust. |
| Codex | `DEGRADED` | Global instruction file is active, but the hook is untrusted or hooks are disabled | Run `/hooks` and trust the stable command definition. Explicitly report the missing reinforcement. |
| Codex | `UNSUPPORTED` | `--no-context-files` or conflicting higher-priority policy defeats activation | Remove the bypass or conflict; managed policy cannot be overridden. |
| Claude | `SUPPORTED` | User output style plus reminder-only `UserPromptSubmit` hook under `$CLAUDE_CONFIG_DIR` | `keep-coding-instructions: true` is mandatory. |
| Claude | `DEGRADED` | Project/local style, same-name project style, or managed policy overrides the user scalar | Report the winning source; do not mutate higher-priority policy. |
| Claude | `UNSUPPORTED` | `--safe-mode`, `--bare`, or managed-only/disabled hooks removes a required path | Remove the explicit bypass or conflict; never report a false pass. |

Absolute always-on behavior cannot defeat explicit host bypasses or higher-priority managed policy. Every probe and acceptance case returns `SUPPORTED`, `DEGRADED`, or `UNSUPPORTED` with a reason. Configuration roots respect `PRIME_AGENT_CODING_AGENT_DIR`, `CODEX_HOME`, and `CLAUDE_CONFIG_DIR`.
