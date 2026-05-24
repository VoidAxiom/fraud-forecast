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
    [ -n "$arg" ] || { echo "usage: review-gate.sh wait <pr> [ackWaitSec] [verdictMaxSec]" >&2; exit 2; }
    # Two-phase, dead-simple eye-emoji loop:
    #   Phase 1 (ack): wait ackWaitSec (default 120s = 2 min) after the
    #     LATEST `@codex review` request was posted, then check whether
    #     codex 👀-acknowledged it. If not, post a fresh `@codex review`
    #     and repeat. Once 👀 lands, move to phase 2.
    #   Phase 2 (verdict): poll every 30s until codex produces FINDINGS or
    #     a clean verdict, or until verdictMaxSec (default 1800s = 30 min)
    #     elapses. No further re-triggers in phase 2.
    #
    # The impl calls `wait` ONCE per pushed head. No outer retry loop in
    # the impl; this helper owns the entire post→ack→verdict cycle.
    ACK_WAIT="${3:-120}"
    VERDICT_MAX="${4:-1800}"
    INT=30
    WQ='query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){mergeStateStatus headRefOid reviews(last:30){nodes{author{login} commit{oid}}} comments(last:50){nodes{author{login} body createdAt reactions(first:20){nodes{content user{login}}}}} reviewThreads(first:100){nodes{isResolved}}}}}'
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
    # ── Unified single loop. Each tick (every $INT seconds) fetches state
    # and decides ONE action:
    #
    #   1. If a terminal verdict has landed (FINDINGS / REVIEWED-CLEAN /
    #      CLEAN-COMMENT-MANUAL) for the current head → exit with that.
    #      This check is FIRST, so a clean verdict that arrives BEFORE
    #      codex 👀-acks the latest request (codex sometimes skips 👀 on
    #      duplicate requests and just replies on the original) is not
    #      missed.
    #
    #   2. Else if codex has 👀-acked the latest non-codex `@codex
    #      review` request → keep waiting (codex is processing).
    #
    #   3. Else if (now - latest request createdAt) ≥ ACK_WAIT → post a
    #      fresh `@codex review` (codex appears to have missed it).
    #
    #   4. Else → just sleep $INT (still inside grace window since the
    #      last request).
    #
    #   5. After $VERDICT_MAX total wall-clock with no verdict → exit
    #      ACK-TIMEOUT (escalate; do NOT spam more requests).
    INT=30
    elapsed=0
    while :; do
      ci="$(gh pr checks "$arg" --json bucket -q '.[0].bucket' 2>/dev/null || echo '?')"
      resp="$(gh api graphql -F o="$OWNER" -F r="$REPO" -F n="$arg" -f query="$WQ" 2>/dev/null)"
      decision="$(printf '%s' "$resp" | CI="$ci" BASE="${BASE_CODEX:-0}" BASE_TS="${BASE_TS:-}" ACK_WAIT="$ACK_WAIT" python3 -c '
import json,os,sys,re,datetime
try:
    d=json.load(sys.stdin)["data"]["repository"]["pullRequest"]
except Exception:
    print("ERR retry"); sys.exit()
CODEX_LOGINS=("chatgpt-codex-connector","chatgpt-codex-connector[bot]")
th=d["reviewThreads"]["nodes"]
openn=sum(1 for t in th if not t["isResolved"])
mss=d["mergeStateStatus"]; ci=os.environ.get("CI","?")
head=d.get("headRefOid")
revs=(d.get("reviews") or {}).get("nodes") or []
review_on_head=any((r.get("author") or {}).get("login") in CODEX_LOGINS
            and ((r.get("commit") or {}).get("oid"))==head for r in revs)
coms=(d.get("comments") or {}).get("nodes") or []
codex_coms=[c for c in coms if (c.get("author") or {}).get("login") in CODEX_LOGINS]
base=int(os.environ.get("BASE","0")); fresh=len(codex_coms)-base
base_ts=os.environ.get("BASE_TS","")
rr=[c for c in coms
    if "@codex review" in (c.get("body") or "").lower()
    and (c.get("author") or {}).get("login") not in CODEX_LOGINS]
latest_rr=max(rr, key=lambda c: c.get("createdAt") or "") if rr else None
rr_anchor=(latest_rr.get("createdAt") or "") if latest_rr else None
clean_comment=bool(rr_anchor) and any(
            (c.get("author") or {}).get("login") in CODEX_LOGINS
            and re.search(r"(did not|didn.?t) find any major issues", c.get("body") or "", re.I)
            and (c.get("createdAt") or "")>rr_anchor
            and (not base_ts or (c.get("createdAt") or "")>base_ts)
            for c in coms)
# Also detect a clean comment that post-dates the wait BASELINE even if it
# pre-dates the latest @codex review request — codex sometimes returns a
# clean verdict against the FIRST request and skips 👀-acking duplicates.
# If the post-baseline clean comment exists AND there are no unresolved
# threads AND merge state is clean, that verdict still applies.
clean_comment_baseline=bool(base_ts) and any(
            (c.get("author") or {}).get("login") in CODEX_LOGINS
            and re.search(r"(did not|didn.?t) find any major issues", c.get("body") or "", re.I)
            and (c.get("createdAt") or "")>base_ts
            for c in coms)
# 1. Terminal verdicts — exit immediately.
if openn>0:
    print("EXIT|FINDINGS open=%d mss=%s ci=%s" % (openn,mss,ci))
    sys.exit()
if review_on_head and fresh>0 and ci!="pending":
    print("EXIT|REVIEWED-CLEAN review_on_head=1 fresh=%d open=0 mss=%s ci=%s" % (fresh,mss,ci))
    sys.exit()
if (clean_comment or clean_comment_baseline) and ci!="pending":
    print("EXIT|CLEAN-COMMENT-MANUAL clean_comment=1 open=0 mss=%s ci=%s — comment-only clean note, NOT a head-pinned verdict (no SHA in body, no head-pinned review). Director must manually confirm this answered an @codex review issued AFTER the current head was pushed." % (mss,ci))
    sys.exit()
# 2/3/4. Non-terminal. Check 👀 + grace window.
acked=False
if latest_rr:
    for r in ((latest_rr.get("reactions") or {}).get("nodes") or []):
        if (r.get("content") or "").upper()=="EYES" and (((r.get("user") or {}).get("login") or "") in CODEX_LOGINS):
            acked=True; break
if acked:
    print("WAIT|ACK-WAITING — codex 👀-acked %s; verdict in flight." % rr_anchor)
    sys.exit()
# Not acked. Compute age of latest request.
ack_wait=int(os.environ.get("ACK_WAIT","120"))
if not latest_rr:
    print("POST|NO-REQUEST — no @codex review request yet; posting first.")
    sys.exit()
try:
    rr_dt=datetime.datetime.strptime(rr_anchor,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    now=datetime.datetime.now(datetime.timezone.utc)
    age=int((now-rr_dt).total_seconds())
except Exception:
    age=0
if age>=ack_wait:
    print("POST|NO-ACK latest=%s age=%ds — past %ds grace window; posting fresh @codex review." % (rr_anchor,age,ack_wait))
else:
    print("WAIT|GRACE latest=%s age=%ds — within %ds grace window for codex 👀." % (rr_anchor,age,ack_wait))
')"
      action="${decision%%|*}"
      message="${decision#*|}"
      echo "t=${elapsed}s [${action}] ${message}"
      case "$action" in
        EXIT) exit 0 ;;
        POST)
          gh pr comment "$arg" --body "@codex review" >/dev/null || {
            echo "failed to post @codex review; aborting" >&2; exit 1; }
          ;;
        WAIT|ERR) : ;;
      esac
      [ "$elapsed" -ge "$VERDICT_MAX" ] && { echo "VERDICT-TIMEOUT after ${VERDICT_MAX}s — never landed a terminal verdict; escalate to Claude (do NOT spam more @codex review requests)."; exit 0; }
      sleep "$INT"; elapsed=$((elapsed+INT))
    done
    ;;

  *)
    echo "usage: review-gate.sh {status|threads|reply|resolve|wait} <pr|threadId> [body|maxSec]" >&2
    exit 2
    ;;
esac
