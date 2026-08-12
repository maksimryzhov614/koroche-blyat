# Koroche Blyat Design Errata

Status: approved implementation clarification for release 1.0.0. This document clarifies the approved design; it does not expand release scope.

## Required interpretations

1. "Always Russian" governs chat framing. An explicitly requested artifact may use its requested language, but the artifact remains clean; this is not a persistent style-off switch.
2. Policy sources, clean-room references, fixtures, raw eval evidence, and the proper name `koroche-blyat` may quote vocabulary they define or measure. The public-artifact gate scans model-authored requested artifacts.
3. User-supplied profanity inside a protected span remains byte-exact. Newly added profanity outside protected spans is forbidden in clean scopes.
4. "Always on" means ordinary supported launches. Explicit bypasses and higher-priority managed or project policy report `DEGRADED` or `UNSUPPORTED`; they never produce a false pass.
5. Scheduled cleanliness is selected only by an observable `<scheduled-task>` marker or `KOROCHE_BLYAT_UNATTENDED=1`; print, JSON, and RPC modes alone do not imply unattended work.
6. Runtime installer modules support Python 3.9 without third-party dependencies. Development and eval tooling may use the pinned project dependencies.
7. Release 1.0 has no persistent style-off switch, selectable intensity levels, Wenyan mode, or deterministic post-generation rewriter. Global deactivation is uninstall.

## Canonical extraction markers

The core uses exactly one nested marker pair, each marker on its own LF line:

```text
<!-- ALWAYS_ON_CORE:BEGIN -->
<!-- ALWAYS_ON_REMINDER:BEGIN -->
<!-- ALWAYS_ON_REMINDER:END -->
<!-- ALWAYS_ON_CORE:END -->
```

The reminder is exactly one non-empty line inside the core. Release 1.0 Codex and Claude hooks are byte-identical, reminder-only `UserPromptSubmit` scripts. Additional `SessionStart` or `SubagentStart` hooks require continuation evidence and an explicit future specification change.
