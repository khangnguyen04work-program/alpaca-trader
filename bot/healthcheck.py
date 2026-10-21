"""Daily health check for the trading system (runs on GitHub Actions).

Fails loudly (GitHub emails the owner when a scheduled workflow fails) when:
  • Alpaca credentials no longer work (rotated/revoked keys, deleted account)
  • a trading workflow's schedule has been disabled by GitHub
  • the crypto loop (24/7) hasn't had a SUCCESSFUL run in the last 3 hours
  • the equity / journal workflow hasn't succeeded in the last 4 days
  • the weekend workflow hasn't succeeded in the last 8 days
  • account equity is zero or missing

It also files a GitHub issue while unhealthy and closes it automatically once
the system is healthy again — so a SILENT stop (no runs at all, which GitHub's
own failure emails cannot detect) becomes visible.

READ-ONLY: places no orders, changes no account settings. Safe to run anytime.

Env: APCA_API_KEY_ID / APCA_API_SECRET_KEY (Alpaca), GITHUB_TOKEN +
GITHUB_REPOSITORY (Actions-provided). HEALTHCHECK_DRY_RUN=1 skips issue filing.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "khangnguyen04work-program/alpaca-trader")
DRY = os.environ.get("HEALTHCHECK_DRY_RUN") == "1"
API = "https://api.github.com"

# workflow file -> max hours since its last successful run
# crypto: GitHub's throttled scheduler delivers 2-5h gaps on this repo (documented
# platform issue for new repos) — a 3h window false-alarms on healthy days.
WINDOWS_H = {"crypto.yml": 6, "trader.yml": 96, "journal.yml": 96, "weekend.yml": 192}

# Workflows that must ALSO show AUTONOMOUS (schedule-triggered) runs. This catches
# a broken/never-registered cron even while manual dispatches still succeed.
SCHEDULE_WINDOWS_H = {"crypto.yml": 6, "trader.yml": 96, "journal.yml": 96}

problems = []


def check(label, ok, detail=""):
    print(f"{'OK  ' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        problems.append(f"{label}" + (f" ({detail})" if detail else ""))


def gh_get(path, tries=3):
    """GET with retries — transient truncation (IncompleteRead) must not turn a
    healthy system into a false alarm."""
    import time
    last = None
    for i in range(tries):
        try:
            headers = {"Accept": "application/vnd.github+json"}
            if TOKEN:
                headers["Authorization"] = f"Bearer {TOKEN}"
            req = urllib.request.Request(API + path, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise last


def gh_write(method, path, payload):
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(payload).encode(),
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


# ── 1. Alpaca: credentials + account sanity ──────────────────────────────────
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rest_client as rc  # noqa: E402
    acct = rc.get_account()
    if isinstance(acct, dict) and "_error" not in acct:
        eq = float(acct.get("equity", 0) or 0)
        check("Alpaca credentials + API reachable", True,
              f"account {acct.get('account_number')} equity ${eq:,.2f}")
        check("Account equity > 0", eq > 0, f"equity ${eq:,.2f}")
    else:
        check("Alpaca credentials + API reachable", False, str(acct)[:140])
except Exception as e:  # noqa: BLE001
    check("Alpaca credentials + API reachable", False, repr(e)[:140])

# ── 1b. Repo age (a brand-new repo gets a grace period: history can't exist) ──
try:
    info = gh_get(f"/repos/{REPO}")
    repo_age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(
        str(info.get("created_at")).replace("Z", "+00:00"))).total_seconds() / 3600.0
except Exception:  # noqa: BLE001
    repo_age_h = 10 ** 6

# ── 2. Workflows still enabled (catches GitHub's 60-day auto-disable) ────────
try:
    wfs = gh_get(f"/repos/{REPO}/actions/workflows").get("workflows", [])
    by_name = {w["path"].split("/")[-1]: w for w in wfs}
    for fname in WINDOWS_H:
        w = by_name.get(fname)
        if not w:
            check(f"{fname}: workflow exists", False, "not found in repo")
        else:
            check(f"{fname}: workflow enabled", w.get("state") == "active",
                  f"state={w.get('state')}")
except Exception as e:  # noqa: BLE001
    check("GitHub workflows API reachable", False, repr(e)[:140])

# ── 3. Recent SUCCESSFUL runs within each workflow's window ──────────────────
try:
    runs = gh_get(f"/repos/{REPO}/actions/runs?per_page=100").get("workflow_runs", [])
    now = datetime.now(timezone.utc)
    for fname, hours in WINDOWS_H.items():
        cutoff = now - timedelta(hours=hours)
        newest = None
        for r in runs:
            if not str(r.get("path", "")).endswith(fname):
                continue
            if r.get("conclusion") != "success":
                continue
            try:
                t = datetime.fromisoformat(str(r.get("created_at", "")).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            if t >= cutoff:
                newest = t
                break
        if newest:
            age = (now - newest).total_seconds() / 3600.0
            check(f"{fname}: successful run within {hours}h", True, f"last {age:.1f}h ago")
        elif repo_age_h < hours:
            check(f"{fname}: successful run within {hours}h", True,
                  f"graced — repo is only {repo_age_h:.1f}h old")
        else:
            check(f"{fname}: successful run within {hours}h", False, "no successful run in window")

    for fname, hours in SCHEDULE_WINDOWS_H.items():
        cutoff = now - timedelta(hours=hours)
        newest = None
        for r in runs:
            if not str(r.get("path", "")).endswith(fname):
                continue
            if r.get("event") != "schedule" or r.get("conclusion") != "success":
                continue
            try:
                t = datetime.fromisoformat(str(r.get("created_at", "")).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            if t >= cutoff:
                newest = t
                break
        label = f"{fname}: AUTONOMOUS (cron) success within {hours}h"
        if newest:
            age = (now - newest).total_seconds() / 3600.0
            check(label, True, f"cron fired {age:.1f}h ago")
        elif repo_age_h < hours:
            check(label, True, f"graced — repo is only {repo_age_h:.1f}h old "
                               f"(cron registration can lag ~1h)")
        else:
            check(label, False, "cron has not fired a successful run in the window — "
                                "schedule may not be registered")
except Exception as e:  # noqa: BLE001
    check("GitHub runs API reachable", False, repr(e)[:140])

# ── 4. Verdict + issue bookkeeping ───────────────────────────────────────────
print()
if problems:
    print("PROBLEMS FOUND:")
    for p in problems:
        print("  -", p)
    if TOKEN and not DRY:
        try:
            body = ("The daily health check failed:\n\n"
                    + "\n".join(f"- {p}" for p in problems)
                    + f"\n\nLatest run: https://github.com/{REPO}/actions/workflows/healthcheck.yml\n\n"
                    "This issue closes automatically once the check passes again. "
                    "If the problem is a credential change, update the repo secrets "
                    "`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`.")
            open_issues = gh_get(f"/repos/{REPO}/issues?state=open&per_page=100")
            existing = [i for i in open_issues
                        if str(i.get("title", "")).startswith("[health-check]")]
            if existing:
                gh_write("POST", f"/repos/{REPO}/issues/{existing[0]['number']}/comments", {"body": body})
                print("-> commented on the existing health-check issue")
            else:
                gh_write("POST", f"/repos/{REPO}/issues",
                         {"title": "[health-check] Trading system unhealthy", "body": body})
                print("-> filed a new health-check issue")
        except Exception as e:  # noqa: BLE001
            print("issue management failed:", repr(e)[:160])
    elif DRY:
        print("(dry run — no issue filed)")
    sys.exit(1)

print("ALL CHECKS PASSED")
if TOKEN and not DRY:
    try:
        open_issues = gh_get(f"/repos/{REPO}/issues?state=open&per_page=100")
        for i in open_issues:
            if str(i.get("title", "")).startswith("[health-check]"):
                gh_write("PATCH", f"/repos/{REPO}/issues/{i['number']}", {"state": "closed"})
                print(f"-> closed health-check issue #{i['number']} (system healthy again)")
    except Exception as e:  # noqa: BLE001
        print("issue close failed:", repr(e)[:160])
sys.exit(0)
