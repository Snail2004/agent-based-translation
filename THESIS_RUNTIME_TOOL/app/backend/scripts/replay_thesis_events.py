from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.thesis_runs import RunRegistry, _event_log_path  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a fixture event JSONL through the real run registry path.")
    parser.add_argument("--jobs-root", default="data/jobs")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--run-id", default="replay_one_button_ui1")
    parser.add_argument("--speed", type=float, default=10.0)
    parser.add_argument("--instant", action="store_true")
    args = parser.parse_args()

    jobs_root = Path(args.jobs_root).resolve()
    fixture = Path(args.fixture).resolve()
    if not fixture.exists():
        raise SystemExit(f"fixture not found: {fixture}")

    registry = RunRegistry(runs_root=jobs_root)
    event_path = _event_log_path(jobs_root, args.run_id)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    if event_path.exists():
        event_path.unlink()
    entry = registry.create_run(
        script="snapshot_runs",
        argv=[sys.executable, "-m", "app.backend.scripts.replay_thesis_events"],
        run_id=args.run_id,
        event_log_path=str(event_path),
    )
    registry.update_run(entry["run_id"], status="running", pid=None)

    if args.instant:
        shutil.copyfile(fixture, event_path)
    else:
        delay = 0.05 / max(args.speed, 0.1)
        with fixture.open("rb") as src, event_path.open("ab") as dst:
            for line in src:
                dst.write(line)
                dst.flush()
                time.sleep(delay)
    registry.update_run(args.run_id, status="done", exit_code=0)
    print(json.dumps({"run_id": args.run_id, "event_log_path": str(event_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

