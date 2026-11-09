#!/usr/bin/env python3
"""Availability monitor for deployments. Standard library only (Python 3.9+).

Runs as its own process, separate from the deployment, and only ever requests /health:
the generation API costs money and must never be called from here.

    monitor.py run          request the URL every second and append to requests.jsonl
    monitor.py wait-healthy block until the running monitor has seen healthy responses
    monitor.py mark NAME    record a deployment event (deploy_start, deploy_end)
    monitor.py wait-exit    block until the monitor has finished its closing period
    monitor.py summary      print the Markdown report
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import http.client
import json
import os
import signal
import statistics
import sys
import time
import urllib.parse
from pathlib import Path

REQUESTS = "requests.jsonl"
EVENTS = "events.jsonl"
PID = "monitor.pid"
# A gap between records longer than this is called out separately in the report.
LONG_GAP_S = 1.5


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


def clock(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch).astimezone().strftime("%H:%M:%S")


def probe(url: urllib.parse.SplitResult, timeout: float) -> dict:
    start = time.time()
    began = time.monotonic()
    status = version = error = None
    ok = False
    conn = http.client.HTTPConnection(url.hostname, url.port or 80, timeout=timeout)
    try:
        conn.request("GET", url.path or "/")
        res = conn.getresponse()
        status = res.status
        body = res.read(4096)
        data = json.loads(body.decode("utf-8"))
        version = data.get("version")
        ok = status == 200 and data.get("status") == "ok" and bool(version)
        if not ok:
            error = "unexpected response"
    except (OSError, http.client.HTTPException, ValueError, AttributeError) as e:
        error = type(e).__name__
    finally:
        conn.close()
    duration = time.monotonic() - began
    return {
        "start": iso(start),
        "end": iso(start + duration),
        "start_epoch": round(start, 3),
        "end_epoch": round(start + duration, 3),
        "duration_ms": round(duration * 1000, 1),
        "http_status": status,
        "ok": ok,
        "version": version,
        "error": error,
    }


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            pass  # a line cut off by a killed process
    return records


def read_events(directory: Path) -> dict:
    return {e["name"]: e["epoch"] for e in read_jsonl(directory / EVENTS)}


def cmd_run(args) -> int:
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / PID).write_text(str(os.getpid()), encoding="utf-8")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    url = urllib.parse.urlsplit(args.url)
    hard_stop = time.monotonic() + args.max_seconds
    closing_at = None
    next_tick = time.monotonic()
    seq = 0
    with open(directory / REQUESTS, "a", encoding="utf-8", buffering=1) as log:
        while True:
            now = time.monotonic()
            if closing_at is None and "deploy_end" in read_events(directory):
                closing_at = now + args.post_seconds
            if now >= hard_stop or (closing_at is not None and now >= closing_at):
                break
            seq += 1
            log.write(json.dumps({"seq": seq, **probe(url, args.timeout)}, ensure_ascii=False) + "\n")
            next_tick += args.interval
            now = time.monotonic()
            if next_tick < now:  # after a slow request, carry on without a catch-up burst
                next_tick = now
            time.sleep(next_tick - now)
    return 0


def monitor_alive(directory: Path) -> bool:
    try:
        os.kill(int((directory / PID).read_text(encoding="utf-8")), 0)
        return True
    except (OSError, ValueError):
        return False


def cmd_wait_healthy(args) -> int:
    directory = Path(args.dir)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        records = read_jsonl(directory / REQUESTS)
        recent = records[-args.count :]
        if len(recent) == args.count and all(r["ok"] for r in recent):
            print(f"Monitor saw {args.count} healthy responses, last version: {recent[-1]['version']}")
            return 0
        if (directory / PID).exists() and not monitor_alive(directory):
            print("Monitor process exited early", file=sys.stderr)
            return 1
        time.sleep(0.25)
    print(f"No {args.count} consecutive healthy responses in {args.timeout:g}s", file=sys.stderr)
    return 1


def cmd_mark(args) -> int:
    directory = Path(args.dir)
    now = time.time()
    with open(directory / EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"name": args.name, "time": iso(now), "epoch": round(now, 3)}) + "\n")
    return 0


def cmd_wait_exit(args) -> int:
    directory = Path(args.dir)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not monitor_alive(directory):
            return 0
        time.sleep(0.5)
    print("Monitor did not stop in time; stopping it", file=sys.stderr)
    try:
        os.kill(int((directory / PID).read_text(encoding="utf-8")), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    return 0


def short(version) -> str:
    return str(version)[:7] if version else "-"


def describe(records: list) -> str:
    """The most common responses in a phase, such as `200 abc1234 (12)`."""
    if not records:
        return "-"
    counts = collections.Counter(
        f"{r['http_status']} {short(r['version'])}" if r["ok"] else (f"{r['http_status']} 실패" if r["http_status"] else str(r["error"]))
        for r in records
    )
    return ", ".join(f"`{name}` ({n})" for name, n in counts.most_common(3))


def longest_outage(records: list):
    """Longest run of failures, from the first failed request to the next success."""
    best = None
    start = None
    for r in records:
        if not r["ok"]:
            if start is None:
                start = r["start_epoch"]
        elif start is not None:
            span = (start, r["start_epoch"])
            if best is None or span[1] - span[0] > best[1] - best[0]:
                best = span
            start = None
    if start is not None:
        span = (start, records[-1]["end_epoch"])
        if best is None or span[1] - span[0] > best[1] - best[0]:
            best = span
    return best


def cmd_summary(args) -> int:
    directory = Path(args.dir)
    records = read_jsonl(directory / REQUESTS)
    events = read_events(directory)
    env = os.environ
    job_status = env.get("DEPLOY_RESULT", "unknown")
    target = env.get("COMMIT_SHA", "")
    fixed_url = env.get("APP_URL", "")
    mode = env.get("DEPLOY_MODE", "")
    failure_reason = env.get("FAILURE_REASON", "")
    colors = f"{env.get('FROM_COLOR')} → {env.get('TO_COLOR')}" if env.get("FROM_COLOR") else env.get("TO_COLOR", "")
    method = {"migration": f"첫 전환: 단일 컨테이너 → 프록시 + {colors}", "bluegreen": f"blue-green: {colors}"}.get(mode, "전환 안 됨")

    healthy = [r for r in records if r["ok"]]
    final_version = healthy[-1]["version"] if healthy and records[-1]["ok"] else None
    if job_status == "success" and final_version == target:
        title = "✅ 배포 성공"
    elif job_status == "success":
        title = "⚠️ 배포 단계는 성공, 최종 버전 불일치"
    elif final_version:
        title = f"❌ 배포 실패: `{short(final_version)}` 서비스 유지"
    else:
        title = "❌ 배포 실패: 서비스 응답 없음"

    out = [f"## {title}", "", "| 항목 | 값 |", "|---|---|"]
    out.append(f"| 고정 주소 | {fixed_url} |")
    out.append(f"| 배포 대상 | `{target}` |")
    out.append(f"| 방식 | {method} |")
    if failure_reason:
        out.append(f"| 실패 이유 | {failure_reason} |")

    if not records:
        out += ["", "모니터링 기록이 없어 가용성을 판단할 수 없습니다."]
        report = "\n".join(out) + "\n"
        sys.stdout.write(report)
        (directory / "summary.md").write_text(report, encoding="utf-8")
        return 0

    first, last = records[0], records[-1]
    out.append(
        f"| 모니터링 범위 | {clock(first['start_epoch'])} ~ {clock(last['end_epoch'])} "
        f"{dt.datetime.now().astimezone().strftime('%Z')} ({last['end_epoch'] - first['start_epoch']:.0f}초) |"
    )

    # Version change
    changes = []
    for prev, cur in zip(healthy, healthy[1:]):
        if prev["version"] != cur["version"]:
            changes.append((prev, cur))
    if changes:
        prev, cur = changes[0]
        change = (
            f"`{short(prev['version'])}` → `{short(cur['version'])}`: {clock(prev['start_epoch'])}에 마지막 이전 버전, "
            f"{clock(cur['start_epoch'])}에 첫 새 버전"
        )
        if len(changes) > 1:
            change += f" (버전 변경 총 {len(changes)}회)"
    else:
        change = f"변경 없음 (`{short(healthy[-1]['version'])}`)" if healthy else "응답 없음"
    out.append(f"| version 변경 | {change} |")

    # Phases
    start_at = events.get("deploy_start", float("inf"))
    end_at = events.get("deploy_end", float("inf"))
    phases = {"배포 전": [], "배포 중": [], "배포 후": []}
    for r in records:
        t = r["start_epoch"]
        phases["배포 전" if t < start_at else "배포 중" if t < end_at else "배포 후"].append(r)
    out += ["", "| 단계 | 성공/전체 | 대표 응답 |", "|---|---:|---|"]
    for name, rows in phases.items():
        out.append(f"| {name} | {sum(r['ok'] for r in rows)}/{len(rows)} | {describe(rows)} |")
    failures = len(records) - len(healthy)
    out.append(f"| **합계** | **{len(healthy)}/{len(records)}** | 실패 {failures}건 |")

    # Gaps: time between one request ending and the next one starting.
    intervals = [b["start_epoch"] - a["start_epoch"] for a, b in zip(records, records[1:])]
    gaps = [(b["start_epoch"] - a["end_epoch"], a["end_epoch"]) for a, b in zip(records, records[1:])]
    out += ["", "| 측정 품질 | 값 |", "|---|---|"]
    if intervals:
        out.append(f"| 실제 요청 간격 | 평균 {statistics.mean(intervals):.2f}초, 최대 {max(intervals):.2f}초 |")
        gap, gap_at = max(gaps)
        long_gaps = sum(1 for g, _ in gaps if g > LONG_GAP_S)
        out.append(f"| 가장 긴 기록 공백 (검증되지 않은 구간) | {gap:.2f}초, {clock(gap_at)} 부터 ({LONG_GAP_S}초 초과 {long_gaps}회) |")
    else:
        gap = 0.0
    durations = [r["duration_ms"] for r in healthy]
    if durations:
        out.append(f"| 응답 시간 | 중앙값 {statistics.median(durations):.0f}ms, 최대 {max(durations):.0f}ms |")

    # Verdict
    caveat = f"요청 사이 공백(최장 {gap:.2f}초)은 검증되지 않은 구간이므로 그보다 짧은 순단까지 없었다고 단정할 수는 없습니다."
    first_transition = mode == "migration" and job_status == "success"
    if failures == 0 and first_transition:
        verdict = (
            f"**첫 전환: 모니터 실패 0건이지만 무중단 판정 대상이 아닙니다.** 단일 컨테이너가 포트를 비운 뒤 프록시가 받는 순간 "
            f"짧은 끊김이 있으며, 1초 간격 측정에는 잡히지 않을 수 있습니다. 무중단 판정은 다음 배포부터 적용됩니다. {caveat}"
        )
    elif failures == 0:
        verdict = f"**관측 범위에서 무중단.** 요청 {len(records)}건이 모두 성공했습니다. {caveat}"
    else:
        outage = longest_outage(records)
        where = f" 가장 긴 장애는 {clock(outage[0])}부터 약 {outage[1] - outage[0]:.1f}초입니다." if outage else ""
        if first_transition:
            verdict = (
                f"**첫 전환이라 끊김 발생: 실패 {failures}건.**{where} 단일 컨테이너가 포트를 비워야 프록시가 받을 수 있어 "
                f"불가피한 구간이며, 다음 배포부터는 blue-green으로 전환됩니다. {caveat}"
            )
        else:
            verdict = f"**무중단 아님: 실패 {failures}건.**{where} {caveat}"
        print(f"::warning::모니터링에서 실패 요청 {failures}건이 기록되었습니다.", file=sys.stderr)
    out += ["", verdict]

    report = "\n".join(out) + "\n"
    sys.stdout.write(report)
    (directory / "summary.md").write_text(report, encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run")
    p.add_argument("--dir", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--post-seconds", type=float, default=30.0)
    p.add_argument("--max-seconds", type=float, default=1400.0)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("wait-healthy")
    p.add_argument("--dir", required=True)
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--timeout", type=float, default=20.0)
    p.set_defaults(func=cmd_wait_healthy)

    p = sub.add_parser("mark")
    p.add_argument("--dir", required=True)
    p.add_argument("name")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("wait-exit")
    p.add_argument("--dir", required=True)
    p.add_argument("--timeout", type=float, default=45.0)
    p.set_defaults(func=cmd_wait_exit)

    p = sub.add_parser("summary")
    p.add_argument("--dir", required=True)
    p.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
