"""Live autoresearch dashboard + sim replays.

    python3 dashboard/server.py [port]        # default 8795

  /            the autoresearch view (same layout as the libero terminal UI), live
  /sim         replays: pick a kept controller version and a scenario
  /replay      ?commit=abc123&seed=7  -> generated on demand, cached
  /api/state   JSON behind the live view

Held-out scores for the baseline and every kept controller are computed here in
the background. The research agent never runs the held-out split.
"""
import calendar
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# ATC_ROOT points the dashboard at another project with the same layout (e.g. if-ai-atc-ksfo)
ROOT = os.path.abspath(os.environ.get("ATC_ROOT") or os.path.dirname(HERE))
# ATC_CACHE_DIR keeps the cache outside the checkout (the repo is pulled from GitHub on deploy)
CACHE_BASE = os.path.abspath(os.environ.get("ATC_CACHE_DIR") or os.path.join(HERE, "cache"))
CACHE = os.path.join(CACHE_BASE, os.path.basename(ROOT))
BATCH = os.environ.get("ATC_BATCH", "v4 realistic aircraft movement")
DEMO = os.path.join(os.path.dirname(ROOT), "if-ai-atc-full")
os.makedirs(CACHE, exist_ok=True)
sys.path.insert(0, ROOT)
from atc import scenarios  # noqa: E402

HOLDOUT = os.path.join(CACHE, "holdout.json")
if not os.path.exists(HOLDOUT) and os.path.basename(ROOT) == "if-ai-atc-full" \
        and os.path.exists(os.path.join(CACHE_BASE, "holdout.json")):
    import shutil
    shutil.copy(os.path.join(CACHE_BASE, "holdout.json"), HOLDOUT)   # keep earlier held-out scores
lock = threading.Lock()


def read(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def rows():
    return [json.loads(line) for line in read(os.path.join(ROOT, "results.jsonl")).splitlines() if line.strip()]


def controller_module(commit):
    """Materialise controller.py at a commit as cache/ctl_<commit>.py."""
    name = f"ctl_{commit}"
    path = os.path.join(CACHE, name + ".py")
    if not os.path.exists(path):
        src = subprocess.run(["git", "show", f"{commit}:controller.py"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout
        with open(path, "w") as f:
            f.write(src)
    return name


def env():
    e = dict(os.environ)
    e["PYTHONPATH"] = CACHE + os.pathsep + ROOT
    return e


def holdout_worker():
    while True:
        try:
            done = json.loads(read(HOLDOUT, "{}"))
            todo = [r["commit"] for r in rows() if r["status"] in ("baseline", "kept") and r["commit"] not in done]
            for commit in todo[:1]:
                mod = controller_module(commit)
                out = subprocess.run([sys.executable, "evaluate.py", "--split", "holdout", "--controller", mod,
                                      "--jobs", "4"], cwd=ROOT, capture_output=True, text=True, env=env()).stdout
                for line in out.splitlines():
                    if line.startswith("JSON "):
                        s = json.loads(line[5:])
                        done[commit] = {"cost": s["cost"], "hard": s["hard"], "rule": s["rule"], "t": time.time()}
                with lock:
                    json.dump(done, open(HOLDOUT, "w"))
        except Exception as e:  # noqa: BLE001
            print("holdout worker:", e, file=sys.stderr)
        time.sleep(10)


MIRROR = os.environ.get("ATC_MIRROR") == "1"   # a read-only copy kept up to date by dashboard/sync.sh

# Merged controllers from the public repo (leaderboard/results.jsonl on upstream main), so the
# leaderboard best gets replays too. Only commits on upstream main are ever materialised and run:
# never pull request code. Each replays with the simulator of its own commit (git archive).
UP_REPO = os.environ.get("ATC_UPSTREAM_REPO") or os.path.join(os.path.dirname(ROOT), "atc-autoresearch")
UP_REF = os.environ.get("ATC_UPSTREAM_REF", "origin/main")
UP_JSON = os.path.join(CACHE, "upstream.json")       # written on the Mac, read by the mirror
_up = {"t": 0, "data": None}


def git_up(*args):
    return subprocess.run(["git", *args], cwd=UP_REPO, capture_output=True, text=True, timeout=60)


def on_main(commit):
    return git_up("merge-base", "--is-ancestor", commit, UP_REF).returncode == 0


def upstream_rows():
    """[{commit, cost, hard, note, best}] for controllers merged into upstream main, oldest first."""
    if MIRROR or not os.path.isdir(UP_REPO):
        return json.loads(read(UP_JSON, "[]") or "[]")
    if _up["data"] is not None and time.time() - _up["t"] < 60:
        return _up["data"]
    out = []
    try:
        best = json.loads(git_up("show", f"{UP_REF}:leaderboard/best.json").stdout or "{}")
        seen = set()
        for line in git_up("show", f"{UP_REF}:leaderboard/results.jsonl").stdout.splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            c = (r.get("commit") or "")[:7]
            if not c.isalnum() or c in seen or not on_main(c):
                continue
            seen.add(c)
            out.append({"commit": c, "cost": r.get("cost"), "hard": r.get("hard"),
                        "note": (r.get("note") or "")[:80], "best": c == (best.get("commit") or "")[:7]})
        with lock:
            json.dump(out, open(UP_JSON, "w"))
    except Exception as e:  # noqa: BLE001
        print("upstream rows:", e, file=sys.stderr)
        out = json.loads(read(UP_JSON, "[]") or "[]")
    _up.update(t=time.time(), data=out)
    return out


def upstream_src(commit):
    """The whole tree of an upstream main commit (simulator + controller), as cache/up-<commit>/."""
    d = os.path.join(CACHE, f"up-{commit}")
    if not os.path.exists(os.path.join(d, "run_demo.py")):
        if not on_main(commit):
            raise ValueError(f"{commit} is not on {UP_REF}")
        tmp = d + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        arc = subprocess.run(["git", "archive", commit], cwd=UP_REPO, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=arc, check=True)
        shutil.rmtree(d, ignore_errors=True)
        os.rename(tmp, d)
    # replays of any commit use main's current viewer, not the one that commit shipped with
    viewer = git_up("show", f"{UP_REF}:atc/replay.html").stdout
    if "/*DATA*/null" in viewer:
        with open(os.path.join(d, "atc", "replay.html"), "w", encoding="utf-8") as f:
            f.write(viewer)
    # and main's current map data (only the session's runway configuration is drawn)
    demo = git_up("show", f"{UP_REF}:run_demo.py").stdout
    if "def world_data" in demo:
        with open(os.path.join(d, "run_demo.py"), "w", encoding="utf-8") as f:
            f.write(demo)
    return d


def agent_alive():
    run = json.loads(read(os.path.join(ROOT, "run.json"), "{}") or "{}")
    if MIRROR:   # the agent runs on the Mac: trust its last heartbeat, if recent
        beat = json.loads(read(os.path.join(ROOT, "mirror.json"), "{}") or "{}")
        return bool(beat.get("alive")) and time.time() - beat.get("t", 0) < 180, run
    pid = run.get("pid")
    if not pid:
        return False, run
    try:
        os.kill(pid, 0)
        return True, run
    except ProcessLookupError:
        return False, run
    except PermissionError:
        return True, run


V0 = os.path.join(os.path.dirname(ROOT), "if-ai-atc", "results.tsv")


def past_batches(own_only=False):
    """Earlier runs, oldest first. Their scores are not comparable with later ones (rules changed).
    own_only: just this project's archived runs (ROOT/runs)."""
    out = []
    if os.path.exists(V0) and not own_only:
        lines = read(V0).splitlines()[1:]
        rows = [{"n": 0, "commit": "e235bfd", "status": "baseline", "idea": "baseline controller (tower only)",
                 "cost": 370.853, "hard": 80, "rule": 12, "go_arounds": 215}]
        for i, line in enumerate(lines, 1):
            f = line.split("\t")
            if len(f) < 6:
                continue
            rows.append({"n": i, "commit": f[0] if f[0] != "reverted" else "-",
                         "status": "reverted" if f[0] == "reverted" else "kept", "idea": f[5],
                         "cost": float(f[1]), "hard": int(f[2]), "rule": int(f[3]), "go_arounds": int(f[4])})
        out.append({"name": "v0 tower only", "note": "ground + tower, no radar; 45 min sessions",
                    "rows": rows, "holdout": {"e235bfd": {"cost": 285.429, "hard": 49},
                                              "58d467b": {"cost": 233.597, "hard": 37}}})
    dirs = []
    for base in ([ROOT] if own_only else dict.fromkeys([DEMO, ROOT])):       # Demo Field history first, then this project's archived runs
        rd = os.path.join(base, "runs")
        if os.path.isdir(rd):
            dirs += [os.path.join(rd, d) for d in sorted(os.listdir(rd))]
    for full in dirs:
        d = os.path.basename(full)
        f = os.path.join(full, "results.jsonl")
        if os.path.exists(f):
            rows = [json.loads(x) for x in read(f).splitlines() if x.strip()]
            meta = json.loads(read(os.path.join(full, "batch.json"), "{}") or "{}")
            demo_hold = {}
            for hp in (os.path.join(CACHE_BASE, "holdout.json"), os.path.join(CACHE_BASE, "if-ai-atc-full", "holdout.json")):
                demo_hold.update(json.loads(read(hp, "{}") or "{}"))
            hold = demo_hold if full.startswith(DEMO) and ROOT != DEMO else None
            if os.path.exists(os.path.join(full, "holdout.json")):
                hold = json.loads(read(os.path.join(full, "holdout.json"), "{}") or "{}")
            out.append({"name": meta.get("name", d), "note": meta.get("note", ""), "rows": rows, "holdout": hold})
    return out


def state():
    alive, run = agent_alive()
    status = json.loads(read(os.path.join(ROOT, "status.json"), "{}") or "{}")
    return {
        "now": time.time(), "name": os.path.basename(ROOT), "alive": alive, "run": run, "status": status,
        "rows": rows(), "holdout": json.loads(read(HOLDOUT, "{}") or "{}"),
        "past": past_batches(own_only=os.environ.get("ATC_SHOW_PAST", "1") == "0"), "current_batch": BATCH,
        "driver": read(os.path.join(ROOT, "autoresearch.log")).splitlines()[-6:],
        "notes": [n for n in read(os.path.join(ROOT, "notes.md")).splitlines() if n.strip()][-14:],
        "community": _community,
    }


UPSTREAM = os.environ.get("ATC_UPSTREAM", "kyleboas/atc-at-home")
# results posted before this time (unix seconds) were scored under earlier rules: left out of the
# contributors' feed and totals. Set it with ATC_BATCH whenever the scoring changes.
FEED_SINCE = float(os.environ.get("ATC_FEED_SINCE", "0") or 0)
TITLE = re.compile(r"^\[(claim|kept|reverted|crash|idea|expired)(?: ([0-9.]+))?\]\s*(.*)$")
_community = {}


def gh_api(path, raw=False):
    gh = shutil.which("gh") or "/opt/homebrew/bin/gh"
    args = [gh, "api", path] + (["-H", "Accept: application/vnd.github.raw"] if raw else [])
    return json.loads(subprocess.run(args, capture_output=True, text=True, timeout=30, check=True).stdout)


def community_worker():
    """Every 5 minutes: contributors' claims and results from the upstream repo's issues.
    Reads issue titles and the leaderboard only; never runs anyone's code."""
    while True:
        try:
            items = gh_api(f"repos/{UPSTREAM}/issues?state=all&per_page=100&sort=created&direction=desc")
            exps = []
            for i in items:
                m = TITLE.match(i.get("title", "")) if "pull_request" not in i else None
                if m and (m.group(1) in ("claim", "idea") or
                          calendar.timegm(time.strptime(i["created_at"], "%Y-%m-%dT%H:%M:%SZ")) >= FEED_SINCE):
                    exps.append({"n": i["number"], "kind": m.group(1), "cost": float(m.group(2)) if m.group(2) else None,
                                 "idea": m.group(3)[:160], "who": (i.get("user") or {}).get("login", "?"),
                                 "t": calendar.timegm(time.strptime(i["created_at"], "%Y-%m-%dT%H:%M:%SZ"))})
            people = {}
            for e in exps:
                if e["kind"] in ("kept", "reverted", "crash"):
                    p = people.setdefault(e["who"], {"who": e["who"], "runs": 0, "kept": 0, "best": None})
                    p["runs"] += 1
                    if e["kind"] == "kept":
                        p["kept"] += 1
                        if e["cost"] is not None and (p["best"] is None or e["cost"] < p["best"]):
                            p["best"] = e["cost"]
            best = gh_api(f"repos/{UPSTREAM}/contents/leaderboard/best.json", raw=True)
            _community.clear()
            _community.update({
                "repo": UPSTREAM, "updated": time.time(),
                "best": {k: best.get(k) for k in ("cost", "hard", "author", "commit", "note")},
                "claims": [e for e in exps if e["kind"] == "claim"],
                "results": [e for e in exps if e["kind"] in ("kept", "reverted", "crash")][:12],
                "people": sorted(people.values(), key=lambda p: (p["best"] is None, p["best"] or 0, -p["runs"]))[:10],
            })
        except Exception:  # noqa: BLE001  (offline, gh missing or rate limited: keep the last data)
            pass
        time.sleep(300)


def replay(commit, seed):
    out = os.path.join(CACHE, f"replay-{commit}-{seed}.html")
    if MIRROR and not os.path.exists(out):
        return ("<!doctype html><meta charset=utf-8><body style='background:#1c1c1e;color:#f2f2f7;font:14px -apple-system,Helvetica,Arial,sans-serif;"
                "padding:20px'>This replay hasn't been made yet. Replays of the current best are added automatically; "
                "<a style='color:#7aa2ff' href='/sim'>back</a>")
    if not os.path.exists(out) and commit in {r["commit"] for r in upstream_rows()}:
        src = upstream_src(commit)
        e = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        subprocess.run([sys.executable, "run_demo.py", "--seed", str(seed), "--quiet", "--commit", commit,
                        "--back", f"/sim?commit={commit}", "--out", out], cwd=src, env=e, capture_output=True, check=True)
    if not os.path.exists(out):
        mod = controller_module(commit)
        subprocess.run([sys.executable, "run_demo.py", "--seed", str(seed), "--controller", mod, "--quiet",
                        "--commit", commit, "--back", "/sim", "--out", out], cwd=ROOT, env=env(),
                       capture_output=True, check=True)
    return read(out)


def configuration(key):
    """'West Plan · land 28L/28R · depart 01L/01R' for a session's runway configuration (older
    simulators have only the West Plan and no configuration: empty)."""
    try:
        from atc import world
        c = world.CONFIGS[key]
    except (ImportError, AttributeError, KeyError):
        return ""
    return f'{c["name"]} · land {"/".join(c["arr"])} · depart {"/".join(c["dep"])}'


def sim_page(q):
    rs = [r for r in rows() if r["status"] in ("baseline", "kept")]
    hold = json.loads(read(HOLDOUT, "{}") or "{}")
    commits = [(r["commit"], r) for r in rs]
    ups = upstream_rows()
    top = next((u["commit"] for u in ups if u["best"]), None)
    pick = q.get("commit", [top or (commits[-1][0] if commits else "HEAD")])[0]
    seeds = scenarios.SPLITS["holdout"][:12] + scenarios.SPLITS["train"][:24]
    opts = "".join(f'<option value="{c}" {"selected" if c == pick else ""}>#{r["n"]} {c} · score {r["cost"]} · hard {r["hard"]}'
                   f' · {html.escape(r["idea"][:60])}</option>' for c, r in reversed(commits))
    up_opts = "".join(f'<option value="{u["commit"]}" {"selected" if u["commit"] == pick else ""}>'
                      f'{"★ leaderboard best" if u["best"] else "main"} {u["commit"]} · score {u["cost"]} · hard {u["hard"]}'
                      f' · {html.escape(u["note"][:60])}</option>' for u in reversed(ups))
    if up_opts:
        opts = f'<optgroup label="public repo (main)">{up_opts}</optgroup><optgroup label="this machine\'s runs">{opts}</optgroup>'
    if pick in {u["commit"] for u in ups}:
        hold = {}
    cards = []
    for sd in seeds:
        sc = scenarios.make(sd)
        n = {k: sum(1 for a in sc["traffic"] if a["intent"] == k) for k in ("departure", "arrival", "pattern")}
        split = "held out" if sd >= 1000 else "train"
        cfg = configuration(sc.get("config"))
        cards.append(f'<a class="card" href="/replay?commit={pick}&seed={sd}"><b>seed {sd}</b> <span>{split} · {sc["level"]} · {round(sc.get("duration", 3600) / 60)} min</span>'
                     + (f'<span class="cfg">{html.escape(cfg)}</span>' if cfg else '')
                     + f'<span>{n["arrival"]} arrivals · {n["departure"]} departures · {n["pattern"]} pattern</span></a>')
    h = hold.get(pick)
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>AI ATC sim</title><style>
body{{margin:0;background:#1c1c1e;color:#f2f2f7;font:13px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}}
header{{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:center;padding:10px 18px;border-bottom:1px solid #38383a;color:#c7c7cc}}
header h1{{margin:0;font-size:15px;font-weight:700;color:#f2f2f7}}
main{{max-width:1180px;margin:0 auto;padding:16px 18px 48px}}
a{{color:#7aa2ff;text-decoration:none}} a:hover{{text-decoration:underline}} .muted{{color:#98989f}}
.btn{{display:inline-block;background:#2c2c2e;color:#f2f2f7;border:1px solid #48484a;border-radius:4px;padding:5px 10px}}
.btn:hover{{background:#5a5a5e;text-decoration:none}}
label{{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#98989f}}
select{{background:#1c1c1e;color:#f2f2f7;border:1px solid #636366;border-radius:4px;padding:5px 6px;font:inherit;font-size:13px;max-width:100%}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;margin-top:16px}}
.card{{display:flex;flex-direction:column;gap:2px;border:1px solid #38383a;border-radius:4px;padding:10px 12px;background:#1c1c1e;color:#f2f2f7}}
.card:hover{{background:#232325;border-color:#636366;text-decoration:none}} .card span{{color:#98989f;font-size:12px}} .card span.cfg{{color:#f2f2f7}}</style>
<header><a class=btn href="/">← Autoresearch</a><h1>Sim replays</h1></header><main>
<p class=muted>Each replay is a full session (40 or 60 minutes, 3 hours for the long peak seeds) at KSFO with ground, tower, approach and departure all run by the chosen controller version.
Generated on first open (a few seconds).</p>
<form method=get action=/sim><label>Controller <select name=commit onchange="this.form.submit()">{opts}</select></label></form>
<p class=muted>{'held out: score ' + str(h['cost']) + ', hard ' + str(h['hard']) if h else 'held-out score pending'}</p>
<div class=grid>{''.join(cards)}</div></main>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="text/html; charset=utf-8", code=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self.send(read(os.path.join(HERE, "index.html")))
            elif u.path == "/api/state":
                self.send(json.dumps(state()), "application/json")
            elif u.path == "/ksfo":
                self.send(read(os.path.join(os.path.dirname(ROOT), "if-ai-atc-ksfo", "ksfo", "map.html"))
                          or "KSFO map not built")
            elif u.path == "/sim":
                self.send(sim_page(q))
            elif u.path == "/replay":
                commit = q.get("commit", [""])[0]
                seed = int(q.get("seed", ["7"])[0])
                if not commit.isalnum():
                    return self.send("bad commit", code=400)
                self.send(replay(commit, seed))
            else:
                self.send("not found", code=404)
        except Exception as e:  # noqa: BLE001
            self.send(f"error: {html.escape(repr(e))}", code=500)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8795
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    threading.Thread(target=community_worker, daemon=True).start()
    if "--no-holdout" not in sys.argv:          # only one server should score held-out runs
        threading.Thread(target=holdout_worker, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
