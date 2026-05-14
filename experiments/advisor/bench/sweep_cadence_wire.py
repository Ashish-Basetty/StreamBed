"""Cadence sweep over the real edge↔advisor UDP wire.

For each cadence N, this script:
  1. Launches advisor_server with --reply-every-n=N (or skips advisor if
     N=='inf', which gives the solo baseline)
  2. Launches edge_inference (with --advisor-host=none if N=='inf')
  3. Runs frame_gen for `--episodes` episodes, capturing its JSON summary
  4. Tears down both processes
  5. Records the row in a sweep summary

Same wire path as Phase C. Same blending mode (replacement). The only
knob varying is the advisor's reply cadence — i.e., how often the
edge gets fresh advice over the wire.

Usage:
  python -m experiments.advisor.bench.sweep_cadence_wire \\
      --student experiments/advisor/checkpoints/students/shared_h4.pt \\
      --teacher experiments/advisor/checkpoints/teacher.zip \\
      --cadences 1 5 10 20 inf \\
      --episodes 10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_UNIFIED_UDP_PORT = 9101
DEFAULT_EDGE_TCP_PORT = 9100


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _advisor_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _wait_for_log(path: Path, needle: str, timeout_s: float) -> bool:
    """Block until the given file contains `needle`, or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if path.exists() and needle in path.read_text():
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def _spawn(cmd: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    f = log_path.open("w")
    return subprocess.Popen(
        cmd, stdout=f, stderr=subprocess.STDOUT,
        cwd=_repo_root(),
        preexec_fn=os.setsid if os.name != "nt" else None,
    )


def _kill(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        else:
            p.terminate()
    except ProcessLookupError:
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        else:
            p.kill()


def run_cadence(
    cadence: int | float,
    args: argparse.Namespace,
) -> dict:
    """Run one cadence point. cadence=math.inf → no advisor."""
    proj = _advisor_root()
    advisor_off = (cadence == math.inf)

    cad_label = "inf" if advisor_off else int(cadence)
    print(f"\n=== cadence={cad_label} ===", flush=True)

    artifact_root = args.artifact_dir or proj
    advisor_log = artifact_root / "logs" / f"sweep_advisor_n{cad_label}.log"
    edge_log = artifact_root / "logs" / f"sweep_edge_n{cad_label}.log"
    frame_gen_out = (
        artifact_root
        / "checkpoints"
        / "eval"
        / f"sweep_n{cad_label}_{args.episodes}ep.json"
    )

    py = sys.executable

    # Start advisor (if applicable).
    advisor_p: subprocess.Popen | None = None
    if not advisor_off:
        advisor_cmd = [
            py, "-u", "-m", "experiments.advisor.server.advisor_server",
            "--teacher", str(args.teacher),
            "--bind-udp", f"0.0.0.0:{args.unified_udp_port}",
            "--reply-every-n", str(int(cadence)),
        ]
        advisor_p = _spawn(advisor_cmd, advisor_log)
        if not _wait_for_log(advisor_log, "duplex listening on", 30.0):
            _kill(advisor_p)
            raise RuntimeError(f"advisor failed to start (see {advisor_log})")

    # Start edge inference.
    edge_cmd = [
        py, "-u", "-m", "experiments.advisor.edge.edge_inference",
        "--teacher", str(args.teacher),
        "--student", str(args.student),
        "--port", str(args.edge_tcp_port),
        "--blend-mode", args.blend_mode,
        "--max-advice-age-s", str(args.max_advice_age_s),
        "--decay-tau-s", str(args.decay_tau_s),
    ]
    if advisor_off:
        edge_cmd += ["--advisor-host", "none"]
    else:
        edge_cmd += [
            "--advisor-host", "127.0.0.1",
            "--advisor-port", str(args.unified_udp_port),
        ]

    edge_p = _spawn(edge_cmd, edge_log)
    if not _wait_for_log(edge_log, "edge_inference TCP listening", 30.0):
        _kill(edge_p)
        if advisor_p is not None:
            _kill(advisor_p)
        raise RuntimeError(f"edge failed to start (see {edge_log})")

    # Run frame_gen — capture its JSON output via --out.
    frame_gen_cmd = [
        py, "-u", "-m", "experiments.advisor.host.frame_gen",
        "--episodes", str(args.episodes),
        "--seed", str(args.seed),
        "--edge-port", str(args.edge_tcp_port),
        "--out", str(frame_gen_out),
    ]
    fg = subprocess.run(
        frame_gen_cmd,
        capture_output=True,
        text=True,
        timeout=args.frame_gen_timeout_s,
        cwd=_repo_root(),
    )
    if fg.returncode != 0:
        _kill(edge_p)
        if advisor_p is not None:
            _kill(advisor_p)
        sys.stderr.write(fg.stdout)
        sys.stderr.write(fg.stderr)
        raise RuntimeError(f"frame_gen failed (rc={fg.returncode})")

    summary = json.loads(frame_gen_out.read_text())

    _kill(edge_p)
    if advisor_p is not None:
        _kill(advisor_p)

    summary["cadence"] = cad_label
    print(f"  score={summary['crafter_score']:.2f} "
          f"unlocks={summary['avg_unlocks_per_ep']:.2f} "
          f"advice_pct={summary['avg_advice_pct']*100:.1f}% "
          f"advice_age_s={summary.get('avg_advice_age_s', -1):.3f} "
          f"rt_ms={summary['avg_rt_ms']:.2f}", flush=True)
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--student", type=Path, required=True)
    p.add_argument("--teacher", type=Path, required=True)
    p.add_argument("--cadences", nargs="+", default=["1", "5", "10", "20", "inf"])
    p.add_argument("--episodes", type=int, default=10)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--blend-mode",
                   choices=["off", "replacement", "additive", "decaying"],
                   default="replacement")
    p.add_argument("--decay-tau-s", type=float, default=0.05,
                   help="Decaying-mode time constant in seconds.")
    p.add_argument("--max-advice-age-s", type=float, default=60.0,
                   help="High default — we want the cadence itself to be "
                        "the limit, not the staleness gate.")
    p.add_argument("--edge-tcp-port", type=int, default=DEFAULT_EDGE_TCP_PORT)
    p.add_argument("--unified-udp-port", type=int, default=DEFAULT_UNIFIED_UDP_PORT,
                   help="Single UDP port for direct bench (advisor --bind-udp, edge --advisor-port).")
    p.add_argument("--frame-gen-timeout-s", type=int, default=600)
    p.add_argument(
        "--artifact-dir",
        type=Path,
        default=None,
        help="Directory for per-cadence logs/results. Default: experiments/advisor.",
    )
    p.add_argument("--out", type=Path,
                   default=_advisor_root() / "checkpoints" / "eval" /
                   "cadence_sweep_wire.json")
    args = p.parse_args()

    cadences = [
        math.inf if c.lower() in {"inf", "infinity", "none"} else int(c)
        for c in args.cadences
    ]

    rows: list[dict] = []
    t0 = time.time()
    for c in cadences:
        try:
            row = run_cadence(c, args)
            rows.append(row)
        except Exception as e:
            print(f"  FAILED at cadence={c}: {e}", file=sys.stderr)
            rows.append({"cadence": "inf" if c == math.inf else int(c),
                         "error": str(e)})

    elapsed = time.time() - t0
    print()
    print("== cadence sweep summary ==")
    print(f"  total wallclock: {elapsed/60:.1f} min")
    print()
    header = f"  {'cadence':>8s}  {'score':>6s}  {'unlocks/ep':>10s}  {'advice%':>8s}  {'age_s':>7s}"
    print(header)
    for r in rows:
        if "error" in r:
            print(f"  {str(r['cadence']):>8s}  ERROR")
            continue
        print(f"  {str(r['cadence']):>8s}  "
              f"{r['crafter_score']:6.2f}  "
              f"{r['avg_unlocks_per_ep']:10.2f}  "
              f"{r['avg_advice_pct']*100:7.1f}%  "
              f"{r.get('avg_advice_age_s', -1):7.3f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "config": {
            "student": str(args.student),
            "teacher": str(args.teacher),
            "episodes": args.episodes,
            "blend_mode": args.blend_mode,
            "max_advice_age_s": args.max_advice_age_s,
        },
        "results": rows,
        "wallclock_s": elapsed,
    }, indent=2))
    print(f"\nsaved -> {args.out}")


if __name__ == "__main__":
    main()
