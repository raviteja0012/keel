"""Pick ONE Jira ticket for the autopilot to work, and emit it for the next step.

One ticket per run, deliberately. A sweep that starts five things finishes none
of them well, and a failure mid-sweep leaves the board in a state nobody can
read. One ticket, one branch, one pull request, one comment.

Selection: tickets in the configured project that are To Do or In Progress,
unassigned or assigned to the autopilot, not already carrying an open autopilot
branch, ordered oldest-updated first so nothing starves.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("JIRA_USER_EMAIL", "")
TOKEN = os.environ.get("JIRA_API_TOKEN", "")
PROJECT = os.environ.get("JIRA_PROJECT_KEY", "")
ONE = (os.environ.get("ONE_ISSUE") or "").strip()

# A ticket whose summary trips these is never picked up unattended. The autopilot
# is told to stop on them anyway, but a model deciding "is this about risk?" is a
# judgement call, and this is a cheap deterministic filter in front of it.
NEEDS_A_PERSON = ("risk", "kill switch", "daily stop", "weekly stop", "go live",
                  "live trading", "promotion gate", "sign-off", "signoff",
                  "credential", "api key", "secret", "ceiling", "delete", "drop table")


def _auth() -> str:
    return "Basic " + base64.b64encode(("%s:%s" % (EMAIL, TOKEN)).encode()).decode()


def call(path: str, params=None):
    url = BASE + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": _auth(), "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def emit(**kw) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        print(kw)
        return
    with open(out, "a") as f:
        for k, v in kw.items():
            if "\n" in str(v):
                f.write("%s<<KEELEOF\n%s\nKEELEOF\n" % (k, v))
            else:
                f.write("%s=%s\n" % (k, v))


def open_autopilot_branches() -> set:
    out = subprocess.run(["git", "ls-remote", "--heads", "origin", "claude/auto-*"],
                         capture_output=True, text=True).stdout
    return {line.rsplit("/", 1)[-1].replace("auto-", "")
            for line in out.splitlines() if line.strip()}


def main() -> int:
    if not (BASE and TOKEN and PROJECT):
        emit(found="false")
        print("not configured")
        return 0

    if ONE:
        jql = "key = %s" % ONE
    else:
        jql = ('project = %s AND statusCategory != Done '
               'ORDER BY updated ASC' % PROJECT)

    try:
        data = call("/rest/api/3/search/jql",
                    {"jql": jql, "maxResults": 25,
                     "fields": "summary,description,status,labels,issuetype"})
    except urllib.error.HTTPError as e:
        print("Jira search failed: HTTP %d %s" % (e.code, e.read().decode()[:200]))
        emit(found="false")
        return 0
    except Exception as e:
        print("Jira unreachable: %s" % e)
        emit(found="false")
        return 0

    busy = open_autopilot_branches()
    for issue in data.get("issues", []):
        key = issue["key"]
        if key in busy:
            continue                      # already has a PR waiting on a human
        fields = issue.get("fields", {})
        summary = fields.get("summary", "")
        if any(w in summary.lower() for w in NEEDS_A_PERSON):
            print("skipping %s: %r needs a person" % (key, summary))
            continue

        desc = fields.get("description")
        if isinstance(desc, dict):        # Atlassian document format
            desc = json.dumps(desc)[:4000]
        ticket = "%s: %s\n\n%s" % (key, summary, (desc or "")[:4000])
        emit(found="true", issue_key=key, ticket=ticket)
        print("picked %s: %s" % (key, summary))
        return 0

    emit(found="false")
    print("nothing to work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
