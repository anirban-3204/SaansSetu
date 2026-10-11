"""
Local SaansSetu API for building the dashboard. No AWS needed.

  python serve.py --day 2024-12-07                 replay that morning for every town, then serve it
  python serve.py --day 2024-12-07 --park 06:30    "latest" shows 06:30, the moment the alert goes out

It replays the day through the same code the Lambda runs, keeps the results in memory, and serves the
same endpoints the deployed API has, on http://localhost:8000:

  /towns   /towns/<id>/config   /towns/<id>/run/latest   /towns/<id>/run/morning
  /towns/<id>/accuracy   /towns/<id>/evidence   /towns/<id>/replay   /towns/<id>/replay/<date>
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from core import api, runner
from core.townconfig import load_towns


def newest_models():
    roots = sorted(Path(".").glob("out_v*"), key=lambda p: int(p.name[5:]) if p.name[5:].isdigit() else 0, reverse=True) + [Path("out")]
    return next((r for r in roots if r.is_dir()), None)


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def _answer(self, method):
            url = urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8") if length else None
            status, out = api.handle(store, method, url.path, dict(parse_qsl(url.query)), dict(self.headers), body)
            reply = api.to_lambda_response(status, out)
            data = reply["body"].encode("utf-8")
            self.send_response(status)
            for k, v in reply["headers"].items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._answer("GET")

        def do_POST(self):
            self._answer("POST")

        def do_OPTIONS(self):
            self._answer("OPTIONS")

        def log_message(self, fmt, *args):
            print("  " + fmt % args)
    return Handler


def main():
    if hasattr(sys.stdout, "reconfigure"):   # Hindi text must print even when output is piped to a file on Windows
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Serve a replayed morning on localhost for dashboard work.")
    ap.add_argument("--day", required=True, help="past date to replay, e.g. 2024-12-07")
    ap.add_argument("--park", default="06:30", help='the time "latest" shows (default 06:30). Use "end" for the last run.')
    ap.add_argument("--towns", default="towns", help="folder of town files")
    ap.add_argument("--data", default="saanssetu_data")
    ap.add_argument("--models", default=None, help="results folder from train.py (default: newest out*/)")
    ap.add_argument("--final", action="store_true", help="use the deployment models instead of the backtest models")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    towns, problems = load_towns(args.towns)
    for name, why in problems.items():
        print(f"Skipping {name}: {why}")
    if not towns:
        sys.exit(f"No usable town files in {args.towns}/")
    models = Path(args.models) if args.models else newest_models()
    if models is None:
        sys.exit("No out*/ folder with trained models found. Run train.py first, or pass --models.")
    print(f"Replaying {args.day} for {', '.join(towns)} with models from {models}/ ...")
    store, frames = runner.replay_day(towns, args.data, models, args.day, model_set="final" if args.final else "models",
                                      park_at=None if args.park == "end" else args.park)
    for town_id, runs in frames.items():
        shown = store.get("latest", town_id, "run")
        a = shown["alert"]
        print(f"  {town_id:15} {len(runs)} runs. Showing {shown['clock']['now'][11:16]}: status {shown['status']}, "
              f"alert {a['decision']} ({a['reason_code']}), PT {a['school_flag']['outdoor_pt']}")
        if shown["status"] == "failed":
            print(f"    {json.dumps(shown['steps'][-1])}")
    print(f"\nServing on http://localhost:{args.port}   (try /towns)   Ctrl+C to stop")
    ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(store)).serve_forever()


if __name__ == "__main__":
    main()
