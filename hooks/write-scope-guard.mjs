#!/usr/bin/env node
// PreToolUse guard — enforces the file-scope contract from CLAUDE.md as a
// wall (PreToolUse permissionDecision: 'deny'). Defense-in-depth backstop
// behind the `implementer` subagent's tools-list strip (no Edit/Write/
// MultiEdit) and the per-commit impl-precommit-scope.sh gate.
//
// Templated by orc-temp's init.sh from template.answers:
//   implementer    — single generic implementer-role name (default "implementer")
//   src/**  — comma- or space-separated dir-prefix globs the
//                           impl may write under (default "src/**")
//   fraud-forecast      — used only in the worktree-root inference comment
//
// Allowlists (evaluated against the active-write-root-relative resolved path;
// each prefix means "this dir at the active write root, or anything under it"):
//   Claude (no agent_id):    scripts/** · **/*.test.* · .claude/** · .codex/** ·
//                            .codex-runs/** · hooks/** · docs/** · **/*.md ·
//                            .gitignore
//                            PLUS: ~/.claude/projects/<key>/memory/**
//                            (project memory lives outside the project root)
//                            (.codex-runs/** is per-packet orchestration:
//                            Claude authors spec.md + scope.txt there; impl
//                            writes git_diff.patch + events.jsonl via Bash.)
//   implementer:      EVERYTHING inside the active write root EXCEPT
//                            Claude's exclusive territory: .claude/** ·
//                            .codex/** · hooks/** · docs/** · **/*.md ·
//                            root .gitignore. Claude is sole author of
//                            those files; the impl writes production code,
//                            config, infra, fixtures, tests — everything
//                            else. (Tools-list strip still applies as
//                            defense-in-depth: even with this scope, the
//                            impl subagent has no Edit/Write/MultiEdit and
//                            must dispatch codex exec for any actual write.)
//   any other subagent:      scripts/** · **/*.test.* only
//
// Tier 0 (every caller except Claude-memory writes): the resolved path MUST
// be inside the active write root.
//
// Anti-rot: strict prefix-from-root matching. No substring-anywhere matches.
// No extension-only matches. Path canonicalization (path.resolve) before
// anchoring. Realpath comparison for the memory exception so symlink
// chicanery cannot escape Tier 0.
//
// Fail-open on parse error: the per-commit impl-precommit-scope.sh and the
// pre-PR re-gate from Claude are the authoritative checks; this hook is a
// catch-early on the editor tools and must not block legitimate work due to
// transient parse errors.
import fs from 'node:fs'
import path from 'node:path'

const IMPL_ROLE = "implementer"

// Implementer scope is defined NEGATIVELY: impl can write anywhere inside the
// active write root EXCEPT Claude's exclusive territory (the CLAUDE_ONLY_*
// constants below). This matches the project shape (a Python data-platform
// repo with no single `src/` dir — code lives under `shared/`, `db/`,
// `archival/`, `simulator/`, etc.) and the doctrine that Claude is the sole
// author of agent-behavior surface + authored markdown docs while the impl
// owns all production code, config, infra, and tests.
const CLAUDE_ONLY_DIRS = ['.claude', '.codex', 'hooks', 'docs']
const CLAUDE_ONLY_FILES = ['.gitignore']
const CLAUDE_ONLY_BASENAME_RE = /\.md$/i

const MAIN_PROJECT_ROOT = path.resolve(
  process.env.CLAUDE_PROJECT_DIR || process.cwd(),
)

// Claude's project memory lives at ~/.claude/projects/<project-key>/memory/
// where <project-key> is the absolute project path with every `/` (including
// the leading one) replaced by `-`. Memory is a Claude-managed sibling of
// the project, outside CLAUDE_PROJECT_DIR. Allow Claude (NOT subagents) to
// write there so memory updates don't require a bash-heredoc workaround.
// Anchored to the exact memory directory or descendants only.
//
// HOME is canonicalized via realpath (handles macOS /var → /private/var) so
// the constant matches what `realpath(CLAUDE_MEMORY_DIR)` returns at check
// time. We then verify `realpath(CLAUDE_MEMORY_DIR) === CLAUDE_MEMORY_DIR`
// — if any component between realHome and memory is a symlink, the equality
// fails and the exception is refused. Closes the symlinked-ancestor bypass.
const CLAUDE_MEMORY_DIR = (() => {
  const home = process.env.HOME || process.env.USERPROFILE || ''
  if (!home) return ''
  let realHome
  try { realHome = fs.realpathSync(home) } catch { return '' }
  // Match Claude Code's project-key encoding: replace any non-alphanumeric
  // character with '-'. The harness encodes `/Users/.../fraud-forecast` to
  // `-Users-...-fraud-forecast`. For dirs containing `_` or `.`, both `/`
  // AND those chars collapse to `-` (so e.g. `/foo/bar_baz` becomes
  // `-foo-bar-baz`). Earlier versions only replaced `/`, which made the
  // memory exception silently dead for any project root containing `_` or
  // `.`. Fix: anchored character-class replacement.
  const projectKey = MAIN_PROJECT_ROOT.replace(/[^a-zA-Z0-9]/g, '-')
  return path.resolve(realHome, '.claude', 'projects', projectKey, 'memory')
})()

function isInside(root, candidate) {
  const rel = path.relative(root, candidate)
  return rel === '' || (!!rel && !rel.startsWith('..') && !path.isAbsolute(rel))
}

function findGitRoot(start) {
  let dir = path.resolve(start)
  for (;;) {
    if (fs.existsSync(path.join(dir, '.git'))) return dir
    const parent = path.dirname(dir)
    if (parent === dir) return ''
    dir = parent
  }
}

function realpathOrNull(p) {
  try { return fs.realpathSync(p) } catch { return '' }
}

// Resolve realpath even when the leaf doesn't exist yet (common for new file
// writes). Climb to the nearest existing ancestor, resolve THAT, and
// re-attach the non-existent tail. Returns '' if nothing resolves.
//
// CRITICAL — dangling symlinks fail closed. If the leaf is a symlink whose
// target doesn't exist, naive parent-fallback would happily resolve the
// parent and accept the path — but the actual write would follow the
// symlink to the OUTSIDE target. Detect symlink leaves via lstat and return
// '' so the caller's prefix check trips and falls through to Tier 0 deny.
function realpathOfPathOrParent(p) {
  const direct = realpathOrNull(p)
  if (direct) return direct
  try {
    const st = fs.lstatSync(p)
    if (st.isSymbolicLink()) return ''  // dangling symlink — refuse
  } catch {
    // ENOENT on lstat: leaf doesn't exist at all (no symlink). Safe.
  }
  let dir = path.dirname(p)
  const tail = [path.basename(p)]
  while (dir && dir !== path.dirname(dir)) {
    const realDir = realpathOrNull(dir)
    if (realDir) return path.resolve(realDir, ...tail.reverse())
    tail.push(path.basename(dir))
    dir = path.dirname(dir)
  }
  return ''
}

function canonicalAgentType(raw) {
  if (!raw) return ''
  if (raw === IMPL_ROLE) return IMPL_ROLE
  // Normalize dynamic instance names (e.g. "<impl-role>-<task-slug>") to the
  // canonical role name. The prefix is controlled by the parent's spawn
  // convention; no other role uses it.
  if (raw.startsWith(IMPL_ROLE + '-')) return IMPL_ROLE
  return raw
}

function linkedGitdir(root) {
  const dotGit = path.join(root, '.git')
  let stat
  try { stat = fs.statSync(dotGit) } catch { return '' }
  if (!stat.isFile()) return ''
  let raw
  try { raw = fs.readFileSync(dotGit, 'utf8') } catch { return '' }
  const m = raw.match(/^gitdir:\s*(.+?)\s*$/m)
  if (!m || !m[1]) return ''
  return path.resolve(root, m[1])
}

function linkedWorktreeRootFromGitdir(gitdir) {
  const gitdirBacklink = path.join(gitdir, 'gitdir')
  let raw
  try { raw = fs.readFileSync(gitdirBacklink, 'utf8') } catch { return '' }
  const line = raw.split(/\r?\n/).map(x => x.trim()).find(x => x.length > 0)
  if (!line) return ''
  const gitdirPath = line.replace(/^gitdir:\s*/, '')
  const worktreeGit = realpathOrNull(path.resolve(gitdir, gitdirPath))
  if (!worktreeGit) return ''
  if (!worktreeGit.endsWith(path.sep + '.git')) return ''
  return path.dirname(worktreeGit)
}

function trustedLinkedWorktree(root) {
  const resolvedRoot = path.resolve(root)
  if (isInside(MAIN_PROJECT_ROOT, resolvedRoot)) {
    return { ok: false, reason: 'the main checkout is not an impl write root' }
  }
  const resolvedRootReal = realpathOrNull(resolvedRoot)
  if (!resolvedRootReal) {
    return { ok: false, reason: 'the linked worktree root cannot be resolved' }
  }
  const gitdir = linkedGitdir(resolvedRoot)
  if (!gitdir) {
    return { ok: false, reason: 'the root is not a linked git worktree with a .git file' }
  }
  const trustedWorktreeDir = path.join(MAIN_PROJECT_ROOT, '.git', 'worktrees')
  const gitdirReal = realpathOrNull(gitdir)
  const trustedWorktreeDirReal = realpathOrNull(trustedWorktreeDir)
  if (!gitdirReal || !trustedWorktreeDirReal) {
    return { ok: false, reason: 'the linked worktree gitdir cannot be resolved' }
  }
  if (gitdirReal === trustedWorktreeDirReal || !isInside(trustedWorktreeDirReal, gitdirReal)) {
    return { ok: false, reason: 'the linked worktree is not attached to this main repo' }
  }
  const roundTripRoot = linkedWorktreeRootFromGitdir(gitdirReal)
  if (!roundTripRoot) {
    return { ok: false, reason: 'the linked worktree does not round-trip to the candidate root' }
  }
  if (roundTripRoot !== resolvedRootReal) {
    return { ok: false, reason: 'the linked worktree .git file does not reference this root' }
  }
  return { ok: true, root: resolvedRoot }
}

function resolveWriteRoot(j, isSub, isImplAgent) {
  if (!isSub) return { root: MAIN_PROJECT_ROOT, cwd: MAIN_PROJECT_ROOT }

  // Generic env var name (project-agnostic). Worktrees provisioned by
  // scripts/worktree-new.sh sit at <repo-parent>/.fraud-forecast-worktrees/<name>/
  // and the spawning Claude can set TRUSTED_WORKTREE_ROOT to that path.
  const explicitRootRaw = process.env.TRUSTED_WORKTREE_ROOT || ''
  const hookCwdRaw = j.cwd ? String(j.cwd) : ''

  if (explicitRootRaw) {
    const explicitRoot = path.resolve(explicitRootRaw)
    const trusted = trustedLinkedWorktree(explicitRoot)
    if (!trusted.ok) {
      return {
        error: `paths inside a trusted linked impl worktree ` +
               `(TRUSTED_WORKTREE_ROOT=${explicitRoot}; ${trusted.reason})`,
        resolved: explicitRoot,
      }
    }
    if (!hookCwdRaw) {
      return {
        error: `paths inside the named impl worktree with a hook cwd inside it ` +
               `(TRUSTED_WORKTREE_ROOT=${explicitRoot})`,
        resolved: explicitRoot,
      }
    }
    const hookCwd = path.resolve(hookCwdRaw)
    if (!isInside(explicitRoot, hookCwd)) {
      return {
        error: `paths inside the named impl worktree, with hook cwd also inside it ` +
               `(TRUSTED_WORKTREE_ROOT=${explicitRoot}; cwd=${hookCwd})`,
        resolved: hookCwd,
      }
    }
    return { root: trusted.root, cwd: hookCwd }
  }

  if (hookCwdRaw) {
    const hookRoot = findGitRoot(hookCwdRaw)
    if (hookRoot) {
      const trusted = trustedLinkedWorktree(hookRoot)
      if (trusted.ok) return { root: trusted.root, cwd: path.resolve(hookCwdRaw) }
      return {
        error: `paths inside a trusted linked impl worktree inferred from hook cwd ` +
               `(cwd=${path.resolve(hookCwdRaw)}; ${trusted.reason})`,
        resolved: path.resolve(hookCwdRaw),
      }
    }
  }

  if (isImplAgent) {
    return {
      error: 'write-capable impl sessions must set TRUSTED_WORKTREE_ROOT',
      resolved: hookCwdRaw ? path.resolve(hookCwdRaw) : MAIN_PROJECT_ROOT,
    }
  }

  return {
    error: 'paths inside a trusted linked impl worktree named by ' +
           'TRUSTED_WORKTREE_ROOT, or a hook cwd inside such a worktree',
    resolved: hookCwdRaw ? path.resolve(hookCwdRaw) : MAIN_PROJECT_ROOT,
  }
}

let raw = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', (d) => (raw += d))
process.stdin.on('end', () => {
  const allow = () => process.exit(0)
  try {
    const j = JSON.parse(raw || '{}')
    if (!['Write', 'Edit', 'MultiEdit'].includes(j.tool_name || '')) return allow()
    const rawFp = (j.tool_input && j.tool_input.file_path) || ''
    if (!rawFp) return allow()  // fail open

    const isSub = !!(j.agent_id && String(j.agent_id).length)
    const agentId = isSub ? String(j.agent_id || '') : ''
    const agentTypeRaw = isSub ? String(j.agent_type || '') : ''
    const agentType = canonicalAgentType(agentTypeRaw)
    const isImplAgent = isSub && agentType === IMPL_ROLE
    const who = isSub ? `subagent "${agentId || agentTypeRaw || '?'}"` : 'Claude'

    const rootSelection = resolveWriteRoot(j, isSub, isImplAgent)
    if (rootSelection.error) {
      return deny(who, rootSelection.error, rawFp, rootSelection.resolved)
    }
    const PROJECT_ROOT = rootSelection.root
    const PROJECT_ROOT_PFX = PROJECT_ROOT + path.sep

    if (
      isSub &&
      !path.isAbsolute(rawFp) &&
      rootSelection.cwd &&
      path.resolve(rootSelection.cwd) !== PROJECT_ROOT
    ) {
      return deny(
        who,
        `relative paths only from the named impl worktree root (${PROJECT_ROOT}) ` +
          `or absolute paths inside it`,
        rawFp,
        path.resolve(rootSelection.cwd, rawFp),
      )
    }

    // Canonicalize. Collapses `..` lexically.
    const resolved = path.resolve(PROJECT_ROOT, rawFp)

    // Memory exception (Claude only, not subagents). Writes to
    // ~/.claude/projects/<key>/memory/** allowed BEFORE Tier 0. Memory lives
    // outside the project root by Claude Code's design.
    //
    // BOTH lexical AND realpath containment required. Lexical alone fails
    // open against symlinks INSIDE memory pointing out; realpath alone
    // fails open against symlinks OUTSIDE memory pointing IN.
    // ROOT-IS-SYMLINK guard: refuse the exception if the memory dir itself
    // is a symlink (would let `<memory>/src/App.tsx` write to a project
    // source file via the symlink target).
    // HARDLINK guard: a file inside memory could be a hard link to an
    // out-of-scope file on the same filesystem. Refuse if the resolved
    // target has > 1 hardlink (stat follows symlinks; new files have no
    // inode yet so this naturally skips ENOENT cases).
    if (!isSub && CLAUDE_MEMORY_DIR) {
      let memRootIsRealDir = false
      try {
        const st = fs.lstatSync(CLAUDE_MEMORY_DIR)
        memRootIsRealDir = st.isDirectory()
      } catch {
        // ENOENT — memory dir doesn't exist; exception simply doesn't apply.
      }
      const realMemoryDir = realpathOrNull(CLAUDE_MEMORY_DIR)
      const memoryPathCanonical = realMemoryDir && realMemoryDir === CLAUDE_MEMORY_DIR
      if (memRootIsRealDir && memoryPathCanonical) {
        if (resolved.startsWith(CLAUDE_MEMORY_DIR + path.sep)) {
          const realCandidate = realpathOfPathOrParent(resolved)
          if (realCandidate && realCandidate.startsWith(realMemoryDir + path.sep)) {
            try {
              const st = fs.statSync(resolved)
              if (st.isFile() && st.nlink > 1) {
                // Hardlinked — refuse exception, fall through to Tier 0.
              } else {
                return allow()
              }
            } catch {
              // ENOENT — new file, no hardlink risk; allow.
              return allow()
            }
          }
        }
      }
    }

    // TIER 0 — must be inside the active write root.
    if (resolved !== PROJECT_ROOT && !resolved.startsWith(PROJECT_ROOT_PFX)) {
      return deny(who, 'paths inside the active write root', rawFp, resolved)
    }
    if (resolved === PROJECT_ROOT) {
      return deny(who, 'a file path (not the active write root directory)', rawFp, resolved)
    }

    const rel = path.relative(PROJECT_ROOT, resolved)
    const base = path.basename(rel)

    // STRICT prefix-from-root matcher. No substring-anywhere matching.
    const startsWithDir = (p) => rel === p || rel.startsWith(p + '/')

    const isTest = /\.(test|spec)\.[A-Za-z0-9]+$/.test(base)

    // Tier 1 (any caller): scripts/** and tests anywhere.
    if (startsWithDir('scripts') || isTest) return allow()

    if (!isSub) {
      // Claude scope: agent-behavior surface + docs + markdown anywhere.
      if (/\.md$/i.test(base)) return allow()
      // .gitignore — EXACT-file rule at the repo root. NOT *.gitignore
      // extension-anywhere (would let src/.gitignore through).
      if (rel === '.gitignore') return allow()
      if (
        startsWithDir('.claude') ||
        startsWithDir('.codex') ||
        startsWithDir('.codex-runs') ||
        startsWithDir('hooks') ||
        startsWithDir('docs')
      ) return allow()
      return deny(
        'Claude',
        'scripts/**, **/*.test.*, .claude/**, .codex/**, .codex-runs/**, hooks/**, docs/**, **/*.md, or .gitignore (root only)',
        rawFp,
        resolved,
      )
    }

    // Subagent — per-agent_type scope.
    if (agentType === IMPL_ROLE) {
      // Impl can write anywhere in the active write root EXCEPT Claude's
      // exclusive territory. Strict-prefix dir match + exact-file match +
      // anchored basename match (no substring-anywhere).
      const claudeOnly =
        CLAUDE_ONLY_DIRS.some((d) => startsWithDir(d)) ||
        CLAUDE_ONLY_FILES.some((f) => rel === f) ||
        CLAUDE_ONLY_BASENAME_RE.test(base)
      if (!claudeOnly) return allow()
      return deny(
        who,
        'anywhere in the active write root EXCEPT Claude\'s exclusive ' +
          'territory: .claude/**, .codex/**, hooks/**, docs/**, **/*.md, ' +
          'or root .gitignore (those are Claude-authored; ask Claude to ' +
          'make the change)',
        rawFp,
        resolved,
      )
    }

    return deny(
      who,
      'scripts/** or **/*.test.* (under the active write root)',
      rawFp,
      resolved,
    )
  } catch {
    return allow()  // fail open on parse error; the per-commit + pre-PR gates are authoritative
  }

  function deny(who, allowed, rawFp, resolved) {
    const reason =
      `CARDINAL RULE (CLAUDE.md file-scope contract): ${who} may only ` +
      `create/edit ${allowed}. ` +
      `"${rawFp}" (resolves to "${resolved}") is outside that scope. ` +
      `Dispatch the implementer subagent (Task tool, subagent_type: "${IMPL_ROLE}") ` +
      `to produce code changes via codex exec. No exception, no "just this once."`
    process.stdout.write(
      JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: reason,
        },
      }),
    )
    process.exit(0)
  }
})
