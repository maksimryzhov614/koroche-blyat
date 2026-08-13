# NOTICE

## koroche-blyat 1.0.0

Repository: <https://github.com/maksimryzhov614/koroche-blyat>

Copyright (c) 2026 Koroche Blyat contributors.
Licensed under the MIT License. See [LICENSE](LICENSE).

## Upstream Attribution

This project adapts ideas and techniques from the following upstream projects.
The authors listed below are not affiliated with koroche-blyat.

### Caveman — Julius Brussee

- Repository: <https://github.com/JuliusBrussee/caveman>
- License: MIT (for skills directory); see `skills/koroche-blyat/licenses/caveman-MIT.txt`
- Commit: `099327780ef69ad88c4cfc15c54314579ac367a4`
- Use: nominative attribution only. "Caveman" is used solely for factual
  attribution to the original project and is not the product name, brand, or
  trademark of koroche-blyat.
- Scope: the upstream `engine/`, `proxy/`, `mcp/`, `shrink/`, `browse/`,
  `cavemem` Go core, and `shared/platform/` paths use the Business Source
  License 1.1 (BSL 1.1). No code or content from those paths is included,
  adapted, or executed here. Only the MIT-licensed `skills/` directory informed
  this work.

### Pohuy — Serge Shima

- Repository: <https://github.com/smixs/pohuy>
- License: MIT; see `skills/koroche-blyat/licenses/pohuy-MIT.txt`
- Commit: `cac2698fae1260347d3d8c7efbc1bee98e041f6d`
- Use: nominative attribution only.
- Known limitation: the upstream README states that its lexicon integrates
  material from the unlicensed `nickname76/russian-swears` dictionary. An MIT
  grant cannot cover material the grantor does not own, so this project treats
  Pohuy as an MIT source for its structure and naming only, and never as a
  source of lexicon content. Only the upstream LICENSE text is redistributed
  here; no Pohuy prose, dictionary entry, definition or example is copied.
  Every lexicon item in `skills/koroche-blyat/references/slovar.md` was
  authored independently, which is why this limitation does not propagate.

### russian-swears — nickname76 (excluded)

- Repository: <https://github.com/nickname76/russian-swears>
- License: NOASSERTION (no license file found in upstream repository)
- Commit: `5be4828435629f9e5f966cde5b54d2eb2a5ba7e7`
- Use: excluded. This repository was identified during research, but its text
  content was not inspected or used for authoring and is not copied or
  redistributed. All lexicon material in koroche-blyat was authored
  independently under clean-room discipline. The pinned content hash comes
  from the approved provenance audit and makes the exclusion traceable.

## Clean-Room Statement

The vocabulary, definitions, severity classifications, and example scenes in
`skills/koroche-blyat/references/slovar.md` and related reference files were
authored from scratch based on the approved design contract and observed
baseline evaluation evidence. No text from the `nickname76/russian-swears`
repository was inspected or used during authoring.
