"""GitHub webhook -> divernote. Runs on the coding server (systemd --user atc-hook, port 8796),
reached through the atc.kyleboas.com tunnel at /hooks/github.

When a pull request on the public repo gets the ready-for-review label (it passed the training and
held-out checks), file one divernote issue in the atc-autoresearch project asking an agent to review the diff.
The review stays in divernote; nothing is posted to GitHub and the contributor's code is never run.

The secret GitHub signs deliveries with is in ~/.config/atc-hook-secret.
"""
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.environ.get("ATC_UPSTREAM", "kyleboas/atc-at-home")
SECRET = open(os.path.expanduser("~/.config/atc-hook-secret"), "rb").read().strip()
SEEN = os.path.expanduser("~/.config/atc-hook-seen.txt")
DIVERNOTE = os.path.expanduser("~/.local/bin/divernote")
lock = threading.Lock()


def log(msg):
    print(time.strftime("%F %T ") + msg, flush=True)


def file_review(n, who, title):
    with lock:
        seen = set(open(SEEN).read().split()) if os.path.exists(SEEN) else set()
        if str(n) in seen:
            return log(f"PR #{n}: already filed")
        details = (
            f"Review pull request #{n} on {REPO} by {who}: \"{title}\" (https://github.com/{REPO}/pull/{n}). "
            "It passed the training and held-out checks; decide whether it is safe to merge.\n\n"
            f"Read it only: `gh pr diff {n} --repo {REPO}` and `gh pr view {n} --repo {REPO} --comments`. "
            "Do NOT check it out, run it, or import it: it is untrusted code. Do not comment, approve or merge on GitHub.\n\n"
            "Report in this issue:\n"
            "- what the change does, in plain words, and the FAA JO 7110.65 paragraphs it relies on;\n"
            "- anything that looks like gaming the score instead of controlling better: detecting the scenario or seed, "
            "special cases for particular callsigns or times, reading files or the environment, getting around "
            "tools/ci_guard.py (aliases of simulator modules that are then changed, introspection), huge lookup tables;\n"
            "- whether it only uses the instructions in atc/commands.py and follows program.md;\n"
            "- your recommendation: merge, ask for changes (say which), or reject.")
        issue = [{"title": f"Review atc-at-home PR #{n}: {title}"[:200], "details": details, "state": "queue"}]
        r = subprocess.run([DIVERNOTE, "project-issues", "add", "--project", "atc-autoresearch", "--issues-json", json.dumps(issue),
                            "--json"], capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            with open(SEEN, "a") as f:
                f.write(f"{n}\n")
            log(f"PR #{n}: filed divernote review issue")
        else:
            log(f"PR #{n}: divernote failed: {(r.stderr or r.stdout)[-500:]}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(405, "POST only")

    def do_POST(self):
        if self.path != "/hooks/github":
            return self.reply(404, "not found")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 5_000_000:
            return self.reply(413, "bad size")
        body = self.rfile.read(n)
        sig = self.headers.get("X-Hub-Signature-256", "")
        want = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return self.reply(401, "bad signature")
        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return self.reply(200, "pong")
        try:
            p = json.loads(body)
        except ValueError:
            return self.reply(400, "bad json")
        pr = p.get("pull_request") or {}
        if (event == "pull_request" and p.get("action") == "labeled"
                and (p.get("label") or {}).get("name") == "ready-for-review"
                and (p.get("repository") or {}).get("full_name") == REPO and pr.get("state") == "open"):
            args = (int(pr["number"]), str((pr.get("user") or {}).get("login", "?"))[:40], str(pr.get("title", ""))[:200])
            threading.Thread(target=file_review, args=args, daemon=True).start()
            return self.reply(202, "filing review")
        self.reply(200, "ignored")


if __name__ == "__main__":
    log("listening on 127.0.0.1:8796")
    ThreadingHTTPServer(("127.0.0.1", 8796), Handler).serve_forever()
