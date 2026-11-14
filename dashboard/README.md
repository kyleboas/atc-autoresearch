# dashboard

The live dashboard at https://atc.kyleboas.com. This folder is what the server runs: deploys
pull it from GitHub, so change it here and push.

- `server.py`: the dashboard and sim replays (`python3 dashboard/server.py 8795`). Set
  `ATC_ROOT` to the research project it shows and `ATC_CACHE_DIR` to keep its cache outside
  the checkout. `ATC_MIRROR=1` serves a read-only copy of a project synced from elsewhere.
- `index.html`: the page.
- `github_hook.py`: webhook for pull requests that pass both checks (see the file).

Contributions still change only `controller.py`; this folder is not scored.
