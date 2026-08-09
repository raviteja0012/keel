"""Report the autopilot's outcome to Jira and Slack.

Plain sentences, no jargon: the person reading the ticket wants to know whether
something is waiting for them. Never fails the build -- a broken relay must not
turn a successful run into a red one.
"""
import base64
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
EMAIL = os.environ.get("JIRA_USER_EMAIL", "")
TOKEN = os.environ.get("JIRA_API_TOKEN", "")
KEY = os.environ.get("ISSUE_KEY", "")
VERDICT = os.environ.get("VERDICT", "unknown")
SLACK = os.environ.get("SLACK_WEBHOOK_URL", "")

WORDS = {
    "review-normal": "A pull request is open and waiting for review. Nothing merges "
                     "on its own here.",
    "human-required": "A pull request is open, and it touches the safety rails, so it "
                      "needs a person to read the diff before it can merge.",
    "refuse": "This one needs a person. The change it would have required is not "
              "something the automation is allowed to make by itself.",
    "unknown": "The run finished without reaching a verdict. Worth a look at the "
               "Actions log.",
}


def read_comment() -> str:
    try:
        with open("autopilot-comment.txt") as f:
            return f.read().strip()[:1500]
    except OSError:
        return ""


def post(url: str, body: dict, headers: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def main() -> None:
    note = read_comment()
    text = "%s\n\n%s" % (note, WORDS.get(VERDICT, WORDS["unknown"])) if note \
        else WORDS.get(VERDICT, WORDS["unknown"])

    if BASE and TOKEN and KEY:
        try:
            auth = "Basic " + base64.b64encode(
                ("%s:%s" % (EMAIL, TOKEN)).encode()).decode()
            post("%s/rest/api/3/issue/%s/comment" % (BASE, KEY),
                 {"body": {"type": "doc", "version": 1, "content": [
                     {"type": "paragraph",
                      "content": [{"type": "text", "text": text}]}]}},
                 {"Authorization": auth, "Content-Type": "application/json"})
            print("commented on %s" % KEY)
        except Exception as e:
            print("Jira comment failed (not fatal): %s" % e)

    if SLACK:
        try:
            post(SLACK, {"text": "*Keel autopilot* `%s` - %s\n%s"
                                 % (KEY or "?", VERDICT, text)},
                 {"Content-Type": "application/json"})
            print("relayed to Slack")
        except Exception as e:
            print("Slack relay failed (not fatal): %s" % e)


if __name__ == "__main__":
    main()
