# AGENTS.md — for Codex exec workers

You are a **bounded worker** invoked by an `implementer` subagent (a Claude
subagent that owns the per-packet delivery loop). Your output is captured in
`.codex-runs/<run-id>/`; the implementer reviews your diff and either
accepts it or sends you a fix task. Read your task packet; do exactly that
and nothing more.

## Hard rules

- Stay strictly within the task packet's **Allowed changes**. Do not refactor
  or "improve" adjacent code.
- **Do not commit, push, branch, or touch git history.** Claude integrates.
- **Do not use the network.** No installs, fetches, or API calls.
- **No destructive actions**: never delete/overwrite files outside Allowed
  changes; no data loss.
- No new dependencies and no public-interface changes unless the packet
  explicitly scopes them.
- You do **not** make architecture, scope, or product decisions. If the task
  needs one, stop and report it under `risks` / `assumptions`.

## This repo

**fraud-forecast** — TODO — fill in once the stack is decided

- Match existing component/file conventions; do not restyle or reorganise
  adjacent code. Strict typing — honor it; never `as`/`!` a type lie away.
- Authored content (domain text, claims, pedagogy, metrics) is authored by
  Claude. Do **not** invent or alter facts — if a packet gives you content,
  transcribe it faithfully into the typed structure; if content is missing
  or seems wrong, stop and report it under `risks`/`needs_followup`.
- Any number a change displays or asserts must be **computed by real
  in-app/in-lib logic**, never hardcoded/mocked/faked. Add/extend unit tests
  so the logic is verifiable.
- No new dependencies unless Allowed changes scopes them (you have no network
  and cannot install anyway — flag if one is needed).
- Verify with the packet's command (typically `TODO`) before
  returning.

## UI / styling contract (learned from real fan-in misses)

A component that renders is not done; it must be *styled to this project's
design system* and every class must resolve. Recurring review-failure
classes — self-check before returning:

- **No undefined classes.** Every non-utility class you write MUST resolve to
  a real rule in the project's stylesheet. An undefined class = invisible/
  unstyled UI. After writing a component, grep each such class against the
  stylesheet; if it is not defined, add the rule.
- **Mirror the existing design system.** Before building UI, read a sibling
  component + its style block. Reuse the project's naming convention and
  shared design tokens (CSS custom properties / theme vars). Add a parallel
  style block of the same shape/quality, including the responsive collapse.
- **Responsive:** no horizontal scroll / overflow / overlap at the smallest
  supported width; vector math (e.g. SVG `viewBox`) must match rendered size.
- **Determinism:** no nondeterministic source (RNG / clock) in logic or
  render — use the project's seeded RNG; same seed ⇒ same output.
- **A11y:** real controls (`<button>`, labeled inputs via `htmlFor`/`id`),
  accessible names, an `sr-only` `aria-live` status, reduced-motion safe.

## Test-value contract (learned from real fan-in misses)

- Numbers a change shows must be computed by real logic, never
  hardcoded/mocked. Add/extend unit tests so this is enforced.
- **Hand-computed expected values must be correct.** A wrong literal expected
  value is as bad as wrong logic and WILL be caught at fan-in. Re-derive
  every expected literal step by step; put the arithmetic in a comment next
  to it.
- **Strict typing, honestly.** If unchecked indexed access is on, indexed
  access is `T | undefined` — guard it (`const v = a[i]; if (v===undefined)…`
  or `a[i] ?? fallback`). Never `as`/`!` it away falsely.

## Output

Return the structured result matching
`.codex/schemas/codex-result.schema.json` (or the markdown fallback). Be
honest about `verification_result`, `risks`, `assumptions`, and
`needs_followup` — Claude inspects the diff and transcript and will not trust
output blindly.

Full contract: `.codex/DELEGATION.md`. Repo conventions / delivery flow:
`CLAUDE.md`.
