#!/usr/bin/env bash
# Codex review-gate helper for the GitHub delivery flow (see CLAUDE.md).
#
#   scripts/review-gate.sh status  <pr>          CI checks + merge state + open threads
#   scripts/review-gate.sh threads <pr>          list review threads (id, resolved, body)
#   scripts/review-gate.sh reply   <id> <body>   in-thread audit note (optional)
#   scripts/review-gate.sh resolve <threadId>    resolve one review conversation
#   scripts/review-gate.sh wait    <pr> [maxSec] poll until Codex responds / clean
#
# Read subcommands (status, threads, wait) are side-effect free. `resolve`/
# `reply` perform GraphQL mutations — part of the normal delivery flow, not
# destructive.
#
# `wait` exists so the caller does not hand-roll a slow fixed loop: it polls
# every ~15s and returns the instant Codex has acted (a new review thread, or
# a chatgpt-codex-connector PR comment with CI settled), instead of grinding a
# long deadline.
#
# Hardened against stale-clean shortcuts (see comments in `status` and `wait`):
# - SAFE auto-clean requires a head-PINNED Codex REVIEW (commit.oid == headRefOid).
# - A clean COMMENT ("did not find any major issues") cannot be tied to a head
#   (no SHA in body, no head-pinned review, GitHub.Commit.pushedDate is null on
#   many repos) and is therefore advisory-only — `CLEAN-COMMENT-MANUAL` state.
# - Per-head freshness anchored on `rr_anchor` (most recent `@codex review`
#   REQUEST comment from a non-Codex login, server-set createdAt — immune to
#   `git commit --date` / rebase / clock skew).
# - `wait`'s baseline captures BOTH the Codex-comment count AND the latest
#   Codex-comment timestamp; a fresh clean comment must post-date BOTH.
set -uo pipefail

cmd="${1:-}"
arg="${2:-}"

repo_json="$(gh repo view --json owner,name)"
OWNER="$(printf '%s' "$repo_json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["owner"]["login"])')"
REPO="$(printf '%s' "$repo_json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["name"])')"

# `finding` = the original review comment (first), fetched separately so it is
# never lost no matter how many replies a thread accrues; `recent` = the tail
# (latest state, e.g. a fix reply). Codex re-reviews land as NEW threads, so
# the gate is "zero unresolved Codex threads", not an in-thread re-review.
Q='query($owner:String!,$repo:String!,$pr:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$pr){mergeable mergeStateStatus headRefOid comments(last:50){nodes{author{login} body createdAt}} reviews(last:30){nodes{author{login} state submittedAt commit{oid}}} reviewThreads(first:100){nodes{id isResolved isOutdated finding:comments(first:1){nodes{author{login} body path}} recent:comments(last:20){totalCount nodes{author{login} body}}}}}}}'

case "$cmd" in
  status)
    [ -n "$arg" ] || { echo "usage: review-gate.sh status <pr>" >&2; exit 2; }
    echo "== CI checks =="
    gh pr checks "$arg" || true
    echo
    echo "== merge state =="
    resp="$(gh api graphql -F owner="$OWNER" -F repo="$REPO" -F pr="$arg" -f query="$Q")"
    printf '%s' "$resp" | python3 -c '
import json,sys,re
d=json.load(sys.stdin)["data"]["repository"]["pullRequest"]
th=d["reviewThreads"]["nodes"]
openn=[t for t in th if not t["isResolved"]]
mss=d["mergeStateStatus"]
print("mergeable=%s mergeStateStatus=%s" % (d["mergeable"], mss))
print("review threads: %d total, %d UNRESOLVED" % (len(th), len(openn)))
for t in openn:
    f=((t["finding"]["nodes"] or [{}])[0])
    rec=t["recent"]["nodes"] or [{}]
    last=rec[-1]
    fw=(f.get("author") or {}).get("login","?")
    lw=(last.get("author") or {}).get("login","?")
    body=" ".join((last.get("body") or f.get("body") or "").split())[:140]
    print("  [open] %s  (%d msgs, finding @%s, latest @%s): %s"
          % (t["id"], t["recent"]["totalCount"], fw, lw, body))
head=d.get("headRefOid")
CODEX_LOGINS=("chatgpt-codex-connector","chatgpt-codex-connector[bot]")
revs=(d.get("reviews") or {}).get("nodes") or []
review_on_head=any((r.get("author") or {}).get("login") in CODEX_LOGINS
    and ((r.get("commit") or {}).get("oid"))==head for r in revs)
# Codex signals CLEAN as a top-level COMMENT — a line like "did not find any
# major issues" (also the "didnt" contraction). It is NOT commit-pinned (reviews
# cannot distinguish clean vs findings), so it carries no headRefOid. Anchor its
# freshness to the most recent `@codex review` REQUEST comment from a non-Codex
# login: that createdAt is GitHub-server-set and cannot be backdated by
# `git commit --date` / rebase / clock skew. Our ship flow ALWAYS re-requests
# review with a bare `@codex review` AFTER pushing a head, so a Codex clean
# verdict post-dating the latest request necessarily pertains to the pushed head.
# Fail-safe: if no review-request comment exists, do NOT accept a bare clean
# comment — require the SHA-pinned review_on_head instead.
coms=(d.get("comments") or {}).get("nodes") or []
rr=[(c.get("createdAt") or "") for c in coms
    if "@codex review" in (c.get("body") or "").lower()
    and (c.get("author") or {}).get("login") not in CODEX_LOGINS]
rr_anchor=max(rr) if rr else None
clean_comment=bool(rr_anchor) and any(
    (c.get("author") or {}).get("login") in CODEX_LOGINS
    and re.search(r"(did not|didn.?t) find any major issues", c.get("body") or "", re.I)
    and (c.get("createdAt") or "")>rr_anchor for c in coms)
# SAFE AUTO-CLEAN = a head-PINNED Codex review only (review_on_head:
# commit.oid==headRefOid, un-spoofable). A clean Codex verdict arrives as a
# top-level COMMENT with NO SHA, NO head-pinned review, and pushedDate is
# typically null on private repos — so a clean comment CANNOT be safely tied
# to a head. It is therefore advisory only and NEVER auto-CLEAN: a merge gate
# must stay safe even when a head is pushed without re-requesting review.
# 0 threads alone is never clean (never-reviewed PR has 0) — false-CLEAN guard.
clean = mss=="CLEAN" and len(openn)==0 and review_on_head
if clean:
    print("\nGATE: CLEAN (head-pinned Codex review on %s, 0 unresolved; mergeable once CI green)" % (head or "?")[:9])
elif review_on_head:
    print("\nGATE: BLOCKED (mss=%s, %d unresolved threads)" % (mss, len(openn)))
elif clean_comment and len(openn)==0 and mss=="CLEAN":
    print("\nGATE: CLEAN-COMMENT-MANUAL (NOT a verdict) — Codex posted a comment-only clean note, but GitHub exposes no signal tying it to head %s (no SHA in body, no head-pinned review, pushedDate null) and a stale in-flight review request can make the timestamps look plausible. This gate CANNOT validate it. Safe resolutions: (a) re-run `@codex review` for the current head and wait for a head-pinned review, or (b) the operator independently confirms, from this session, that this clean note answered an `@codex review` issued AFTER this exact head was pushed. Never auto-merge on this." % (head or "?")[:9])
else:
    print("\nGATE: BLOCKED — no head-pinned Codex review on current head %s (mss=%s, %d unresolved). Trigger/await @codex review; do NOT merge." % ((head or "?")[:9], mss, len(openn)))
'
    ;;

  threads)
    [ -n "$arg" ] || { echo "usage: review-gate.sh threads <pr>" >&2; exit 2; }
    resp="$(gh api graphql -F owner="$OWNER" -F repo="$REPO" -F pr="$arg" -f query="$Q")"
    printf '%s' "$resp" | python3 -c '
import json,sys
th=json.load(sys.stdin)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
if not th:
    print("no review threads"); sys.exit()
for t in th:
    first=(t["finding"]["nodes"] or [{}])[0]
    rec=t["recent"]["nodes"] or [{}]
    last=rec[-1]
    n=t["recent"]["totalCount"]
    path=first.get("path") or "-"
    state="resolved" if t["isResolved"] else "OPEN"
    print("%s  [%s] (%s)  %d msg(s)" % (t["id"], state, path, n))
    fw=(first.get("author") or {}).get("login","?")
    print("   finding @%s: %s" % (fw, " ".join((first.get("body") or "").split())[:300]))
    if n > 1:
        lw=(last.get("author") or {}).get("login","?")
        print("   latest  @%s: %s" % (lw, " ".join((last.get("body") or "").split())[:300]))
'
    ;;

  reply)
    # reply <threadId> <body...> — OPTIONAL in-thread note (audit only).
    # The convention is to acknowledge via a TOP-LEVEL `@codex` PR comment
    # highlighting the change, then `resolve` the old thread, then wait for
    # Codex's re-review (which arrives as NEW threads). See CLAUDE.md.
    body="${*:3}"
    { [ -n "$arg" ] && [ -n "$body" ]; } || {
      echo 'usage: review-gate.sh reply <threadId> <body...>' >&2; exit 2; }
    resp="$(gh api graphql -F tid="$arg" -F body="$body" -f query='mutation($tid:ID!,$body:String!){addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$tid,body:$body}){comment{url}}}')"
    printf '%s' "$resp" | python3 -c 'import json,sys;print("replied:",json.load(sys.stdin)["data"]["addPullRequestReviewThreadReply"]["comment"]["url"])'
    ;;

  resolve)
    [ -n "$arg" ] || { echo "usage: review-gate.sh resolve <threadId>" >&2; exit 2; }
    resp="$(gh api graphql -F threadId="$arg" -f query='mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId}){thread{id isResolved}}}')"
    printf '%s' "$resp" | python3 -c 'import json,sys;t=json.load(sys.stdin)["data"]["resolveReviewThread"]["thread"];print("resolved",t["id"],t["isResolved"])'
    ;;

  wait)
    [ -n "$arg" ] || { echo "usage: review-gate.sh wait <pr> [maxSec]" >&2; exit 2; }
    MAX="${3:-360}"
    INT=15
    WQ='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){mergeStateStatus headRefOid reviews(last:30){nodes{author{login} commit{oid}}} comments(last:50){nodes{author{login} body createdAt}} reviewThreads(first:100){nodes{isResolved}}}}}'
    # Capture BOTH the Codex-comment count AND the latest Codex-comment
    # timestamp at invocation. The timestamp is the load-bearing baseline:
    # a fresh clean comment must post-date it (server-side createdAt, no
    # clock skew). A count delta alone is insufficient — an unrelated fresh
    # Codex comment (e.g. a re-request ack) must not let an OLD clean comment
    # satisfy the gate.
    # The baseline MUST be established from a successful fetch; a failed /
    # transient / non-JSON response must NOT default to 0 (that would make
    # historical comments look fresh and reopen the stale-clean shortcut).
    # Retry, then abort rather than guess.
    BASE_CODEX=""
    BASE_TS=""
    for _attempt in 1 2 3 4 5; do
      base_resp="$(gh api graphql -F o="$OWNER" -F r="$REPO" -F n="$arg" -f query="$WQ" 2>/dev/null)"
      parsed="$(printf '%s' "$base_resp" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)["data"]["repository"]["pullRequest"]
    cs=[c for c in d["comments"]["nodes"] if (c.get("author") or {}).get("login") in ("chatgpt-codex-connector","chatgpt-codex-connector[bot]")]
    ts=max((c.get("createdAt") or "" for c in cs), default="")
    print("OK", len(cs), ts or "-")
except Exception:
    print("ERR")')"
      case "$parsed" in
        "OK "*) rest="${parsed#OK }"; BASE_CODEX="${rest%% *}"; BASE_TS="${rest#* }"; break ;;
      esac
      sleep 3
    done
    [ -n "$BASE_CODEX" ] || {
      echo "wait: could not establish Codex-comment baseline after retries — aborting (refusing to risk a stale-clean shortcut)" >&2
      exit 3; }
    [ "$BASE_TS" = "-" ] && BASE_TS=""
    echo "baseline: $BASE_CODEX prior Codex comment(s), latest @ ${BASE_TS:-none} — waiting for a clean verdict newer than that"
    elapsed=0
    while :; do
      ci="$(gh pr checks "$arg" --json bucket -q '.[0].bucket' 2>/dev/null || echo '?')"
      resp="$(gh api graphql -F o="$OWNER" -F r="$REPO" -F n="$arg" -f query="$WQ" 2>/dev/null)"
      verdict="$(printf '%s' "$resp" | CI="$ci" BASE="${BASE_CODEX:-0}" BASE_TS="${BASE_TS:-}" python3 -c '
import json,os,sys,re
try:
    d=json.load(sys.stdin)["data"]["repository"]["pullRequest"]
except Exception:
    print("ERR retry"); sys.exit()
th=d["reviewThreads"]["nodes"]
openn=sum(1 for t in th if not t["isResolved"])
codex=sum(1 for c in d["comments"]["nodes"] if (c.get("author") or {}).get("login") in ("chatgpt-codex-connector","chatgpt-codex-connector[bot]"))
base=int(os.environ.get("BASE","0")); fresh=codex-base
mss=d["mergeStateStatus"]; ci=os.environ.get("CI","?")
# Verdict on the CURRENT head. Codex signals CLEAN as a COMMENT — accept iff
# it post-dates the head commit and the wait baseline (resolve-then-rereview
# on the same commit must not short-circuit). A commit-pinned review still
# counts but the review path also requires a fresh comment since baseline.
# 0 threads alone is never clean (false-CLEAN guard).
head=d.get("headRefOid")
CODEX_LOGINS=("chatgpt-codex-connector","chatgpt-codex-connector[bot]")
review_on_head=any((r.get("author") or {}).get("login") in CODEX_LOGINS
            and ((r.get("commit") or {}).get("oid"))==head
            for r in ((d.get("reviews") or {}).get("nodes") or []))
base_ts=os.environ.get("BASE_TS","")
coms=(d.get("comments") or {}).get("nodes") or []
# Per-head baseline = most recent `@codex review` REQUEST comment from a
# non-Codex login. Our ship flow always re-requests AFTER pushing a head,
# so a clean verdict post-dating it pertains to the pushed head. Also keep
# the wait-baseline (base_ts) guard so a clean comment pre-dating this wait
# cannot short-circuit via resolve-then-rereview. Fail-safe: no
# review-request comment → do not accept a bare clean comment (require
# SHA-pinned review_on_head).
rr=[(c.get("createdAt") or "") for c in coms
    if "@codex review" in (c.get("body") or "").lower()
    and (c.get("author") or {}).get("login") not in CODEX_LOGINS]
rr_anchor=max(rr) if rr else None
clean_comment=bool(rr_anchor) and any(
            (c.get("author") or {}).get("login") in CODEX_LOGINS
            and re.search(r"(did not|didn.?t) find any major issues", c.get("body") or "", re.I)
            and (c.get("createdAt") or "")>rr_anchor
            and (not base_ts or (c.get("createdAt") or "")>base_ts)
            for c in coms)
# Clean path: the fresh clean comment is itself the verdict. Review path:
# require fresh comment too (so resolve-then-rereview on the same head
# cannot short-circuit before Codex actually re-replies).
# SAFE auto-clean = head-PINNED review only (+ fresh-comment guard).
# A clean COMMENT cannot be tied to a head — NEVER auto-clean; exits the
# wait promptly with a DISTINCT status that tells the caller a manual
# head-correspondence check is required before merge.
if openn>0:
    print("FINDINGS open=%d mss=%s ci=%s" % (openn,mss,ci))
elif review_on_head and fresh>0 and ci!="pending":
    print("REVIEWED-CLEAN review_on_head=1 fresh=%d open=0 mss=%s ci=%s" % (fresh,mss,ci))
elif clean_comment and ci!="pending":
    print("CLEAN-COMMENT-MANUAL clean_comment=1 open=0 mss=%s ci=%s — comment-only clean note, NOT a validatable verdict (no SHA / no head-pinned review / pushedDate null; a stale in-flight request can fake plausible timestamps). Re-run @codex review for the current head and wait for a head-pinned review, OR the operator confirms from this session it answered a request issued AFTER this head was pushed. Never auto-merge." % (mss,ci))
else:
    print("WAITING codex=%d fresh=%d clean_comment=%d review_on_head=%d open=%d ci=%s" % (codex,fresh,int(clean_comment),int(review_on_head),openn,ci))
')"
      echo "t=${elapsed}s ${verdict}"
      case "$verdict" in
        FINDINGS*|REVIEWED-CLEAN*|CLEAN-COMMENT-MANUAL*) exit 0 ;;
      esac
      [ "$elapsed" -ge "$MAX" ] && { echo "TIMEOUT after ${MAX}s"; exit 0; }
      sleep "$INT"; elapsed=$((elapsed+INT))
    done
    ;;

  *)
    echo "usage: review-gate.sh {status|threads|reply|resolve|wait} <pr|threadId> [body|maxSec]" >&2
    exit 2
    ;;
esac
