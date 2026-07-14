# Codex project instructions

## Shared project guidance

Before planning or modifying code, read `CLAUDE.md`.

Treat the following parts of `CLAUDE.md` as shared project requirements:

* Project architecture and hardware lifecycle
* Controller ownership and cleanup rules
* Hardware safety constraints
* Platform requirements
* Qt threading requirements
* Internationalisation conventions
* UI conventions
* Required verification and app-specific documentation

Instructions that refer specifically to Claude Code slash commands, Claude
skills, subagents, hooks, or `.claude/commands/` are not directly executable
by Codex. Preserve their underlying engineering intent and read the referenced
implementation documentation when relevant.

Do not modify `CLAUDE.md` or files under `.claude/` unless the task explicitly
concerns agent configuration.

## Documentation loading

Do not read every linked document pre-emptively.

Before making a non-trivial change in a subsystem, read the corresponding
`IMPLEMENTATION_DETAILS.md`, `SPEC.md`, or other document linked from
`CLAUDE.md`.

For hardware-facing changes, identify whether the code can be tested safely in
simulation before running it.

## Planning

Use a detailed plan when the change:

* spans multiple applications or subsystems;
* changes hardware-control behaviour;
* changes threading, controller ownership, or cleanup;
* changes persisted data or file formats;
* affects public UI behaviour; or
* has unclear requirements.

For a small and localised fix, use a brief plan and avoid unnecessary
decomposition.

## Implementation

* Inspect the relevant implementation and tests before editing.
* Prefer the smallest change that fully resolves the problem.
* Do not refactor unrelated code.
* Preserve existing hardware-safety checks.
* Do not infer hardware protocol behaviour that is not documented or tested.
* Use British spelling in user-facing English.
* Follow the Windows-first platform requirement.

## Review and verification

Before reporting completion:

1. Review the final diff independently.
2. Run the narrowest relevant checks available without physical hardware.
3. Use simulation mode where applicable.
4. Check error paths, cleanup, thread boundaries, and hardware-state
   transitions.
5. Confirm that no unrelated files were changed.
6. State which checks were run, which were not run, and why.
7. Report any remaining hardware-dependent assumptions or risks.

When reviewing work produced by another coding agent, inspect the implementation
independently before relying on that agent's explanation.
