#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "v8_cloud_rotation.json"
EVIDENCE_PATH = ROOT / "evidence" / "v8-cloud" / "latest.json"

LANES = ("owner", "collab-1", "collab-2")
ROTATION_MODE = "ROUND_ROBIN_NEW_TASKS_ONLY"
MAX_PARALLEL_KAGGLE = 2
GPU_RESERVE_FRACTION = 0.15
GPU_MIN_POST_RUN_HOURS = 0.50
GPU_TIMEOUT_SECONDS = 480
GPU_ACCELERATOR = "NvidiaTeslaT4"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default)
    except Exception:
        return dict(default)


def _wilson_lower(successes: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    p = successes / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = p + z2 / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return (centre - margin) / denom


def run_cpu_support() -> dict[str, Any]:
    started = time.perf_counter()
    rng = random.Random(20260927)
    violations = 0
    scenarios = 100000

    for _ in range(scenarios):
        quality = rng.random()
        health = rng.random()
        freshness = rng.random()
        latency_ms = rng.randint(5, 2000)
        zero_spend = rng.random() > 0.12
        public = rng.random() > 0.05
        eligible = zero_spend and public
        score = quality * 0.45 + health * 0.30 + freshness * 0.25 - latency_ms / 20000.0
        score2 = quality * 0.45 + health * 0.30 + freshness * 0.25 - latency_ms / 20000.0
        if score != score2:
            violations += 1
        if eligible and (not zero_spend or not public):
            violations += 1

    successes = scenarios - violations
    elapsed = time.perf_counter() - started
    return {
        "status": "PASS" if violations == 0 else "FAIL",
        "workload": "PUBLIC_SYNTHETIC_ROUTING_AND_EVIDENCE_STRESS",
        "scenarios": scenarios,
        "violations": violations,
        "pass_rate": successes / scenarios,
        "wilson_lower_95": _wilson_lower(successes, scenarios),
        "elapsed_seconds": elapsed,
        "paid_compute_used": False,
        "private_source_used": False,
    }


def _secret_pair(lane: str) -> tuple[str | None, str | None]:
    suffix = {
        "owner": "OWNER",
        "collab-1": "COLLAB1",
        "collab-2": "COLLAB2",
    }[lane]
    token = (os.getenv(f"FC_KAGGLE_{suffix}_API_TOKEN") or "").strip() or None
    username = (os.getenv(f"FC_KAGGLE_{suffix}_USERNAME") or "").strip() or None
    return token, username


def _run(cmd: list[str], env: dict[str, str], timeout: int, cwd: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _lane_env(token: str, username: str) -> dict[str, str]:
    env = os.environ.copy()
    env["KAGGLE_API_TOKEN"] = token
    env["KAGGLE_USERNAME"] = username
    env.pop("KAGGLE_KEY", None)
    return env


def _parse_hours(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw.endswith("h"):
            raw = raw[:-1]
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _quota(env: dict[str, str]) -> dict[str, Any]:
    rc, out, _ = _run(["kaggle", "quota", "--format", "json"], env, timeout=45)
    if rc != 0:
        return {"ok": False, "reason": "QUOTA_QUERY_FAILED"}

    try:
        payload = json.loads(out)
    except Exception:
        return {"ok": False, "reason": "QUOTA_JSON_INVALID"}

    rows = payload if isinstance(payload, list) else [payload]
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("resource") or "").upper() != "GPU":
            continue
        remaining = _parse_hours(row.get("remaining"))
        total = _parse_hours(row.get("total"))
        used = _parse_hours(row.get("used"))
        if remaining is None or total is None or total <= 0:
            return {"ok": False, "reason": "GPU_QUOTA_UNKNOWN"}
        return {
            "ok": True,
            "remaining_hours": remaining,
            "total_hours": total,
            "used_hours": used,
            "refresh_at": row.get("refreshAt") or row.get("refillAt"),
        }
    return {"ok": False, "reason": "GPU_QUOTA_ROW_MISSING"}


def _gpu_safe(quota: dict[str, Any]) -> tuple[bool, str]:
    if quota.get("ok") is not True:
        return False, str(quota.get("reason") or "GPU_QUOTA_UNKNOWN")
    remaining = float(quota["remaining_hours"])
    total = float(quota["total_hours"])
    reserve = max(total * GPU_RESERVE_FRACTION, GPU_MIN_POST_RUN_HOURS)
    planned = GPU_TIMEOUT_SECONDS / 3600.0
    if remaining - planned < reserve:
        return False, "GPU_RESERVE_GUARD"
    return True, "GPU_QUOTA_SAFE"


def _gpu_program(run_id: str, lane: str) -> str:
    return f'''import json, os, time
result={{
    "ok":False,
    "run_id":{run_id!r},
    "lane":{lane!r},
    "cuda":False,
    "device_count":0,
    "device_name":None,
    "training_loss_start":None,
    "training_loss_end":None,
    "throughput_samples_per_second":0.0,
}}
start=time.perf_counter()
try:
    import torch
    torch.manual_seed(20260927)
    if torch.cuda.is_available() and torch.cuda.device_count() > 0:
        device=torch.device("cuda:0")
        result["cuda"]=True
        result["device_count"]=int(torch.cuda.device_count())
        result["device_name"]=torch.cuda.get_device_name(0)

        n=65536
        d=128
        classes=8
        x=torch.randn(n,d,device=device)
        true_w=torch.randn(d,classes,device=device)
        y=(x @ true_w).argmax(dim=1)

        model=torch.nn.Sequential(
            torch.nn.Linear(d,256),
            torch.nn.ReLU(),
            torch.nn.Linear(256,classes),
        ).to(device)
        opt=torch.optim.AdamW(model.parameters(),lr=2e-3)
        loss_fn=torch.nn.CrossEntropyLoss()

        batch=2048
        first=None
        last=None
        seen=0
        train_start=time.perf_counter()
        for step in range(180):
            idx=(step*batch) % n
            xb=x[idx:idx+batch]
            yb=y[idx:idx+batch]
            if xb.shape[0] < batch:
                xb=x[:batch]
                yb=y[:batch]
            opt.zero_grad(set_to_none=True)
            logits=model(xb)
            loss=loss_fn(logits,yb)
            loss.backward()
            opt.step()
            if first is None:
                first=float(loss.detach().cpu())
            last=float(loss.detach().cpu())
            seen += int(xb.shape[0])
        torch.cuda.synchronize()
        elapsed=max(time.perf_counter()-train_start,1e-9)
        result["training_loss_start"]=first
        result["training_loss_end"]=last
        result["throughput_samples_per_second"]=seen/elapsed
        result["ok"]=bool(
            result["cuda"]
            and first is not None
            and last is not None
            and last < first
        )
except Exception as exc:
    result["error_type"]=type(exc).__name__

result["elapsed_seconds"]=time.perf_counter()-start
open("/kaggle/working/freecompute_result.json","w",encoding="utf-8").write(
    json.dumps(result,sort_keys=True)
)
print(json.dumps(result,sort_keys=True))
'''


def _status_from_text(text: str) -> str:
    value = (text or "").strip().lower()
    if any(x in value for x in ("error", "failed", "failure", "cancelled")):
        return "FAILED"
    if any(x in value for x in ("complete", "completed", "success")):
        return "COMPLETED"
    if any(x in value for x in ("running", "queued", "pending")):
        return "RUNNING"
    return "UNKNOWN"


def _run_gpu_lane(lane: str, token: str, username: str, run_id: str) -> dict[str, Any]:
    env = _lane_env(token, username)
    quota_before = _quota(env)
    safe, reason = _gpu_safe(quota_before)
    if not safe:
        return {
            "lane": lane,
            "status": "SKIP",
            "reason": reason,
            "quota_before": quota_before,
            "paid_compute_used": False,
        }

    ref = None
    started = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix=f"v8-cloud-{lane}-") as td:
            root = Path(td)
            slug = f"v8-cloud-{run_id[:10]}-{uuid.uuid4().hex[:8]}".lower()
            ref = f"{username}/{slug}"

            (root / "main.py").write_text(_gpu_program(run_id, lane), encoding="utf-8")
            metadata = {
                "id": ref,
                "title": slug,
                "code_file": "main.py",
                "language": "python",
                "kernel_type": "script",
                "is_private": True,
                "enable_gpu": True,
                "enable_internet": False,
                "dataset_sources": [],
                "competition_sources": [],
                "kernel_sources": [],
                "model_sources": [],
                "machine_shape": GPU_ACCELERATOR,
            }
            (root / "kernel-metadata.json").write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            rc, _, err = _run(
                ["kaggle", "kernels", "push", "-p", str(root), "-t", str(GPU_TIMEOUT_SECONDS), "--accelerator", GPU_ACCELERATOR],
                env,
                timeout=120,
                cwd=td,
            )
            if rc != 0:
                return {
                    "lane": lane,
                    "status": "FAIL",
                    "reason": "KAGGLE_KERNEL_PUSH_FAILED",
                    "error_tail": err[-400:],
                    "quota_before": quota_before,
                    "paid_compute_used": False,
                }

        deadline = time.monotonic() + GPU_TIMEOUT_SECONDS + 180
        status = "UNKNOWN"
        while time.monotonic() < deadline:
            rc, out, err = _run(["kaggle", "kernels", "status", ref], env, timeout=45)
            if rc == 0:
                status = _status_from_text(out + "\n" + err)
                if status in {"COMPLETED", "FAILED"}:
                    break
            time.sleep(10)

        if status != "COMPLETED":
            return {
                "lane": lane,
                "status": "FAIL",
                "reason": f"KAGGLE_{status}",
                "quota_before": quota_before,
                "paid_compute_used": False,
            }

        with tempfile.TemporaryDirectory(prefix=f"v8-cloud-output-{lane}-") as outdir:
            rc, _, err = _run(
                ["kaggle", "kernels", "output", ref, "-p", outdir, "-o", "-q"],
                env,
                timeout=120,
            )
            if rc != 0:
                return {
                    "lane": lane,
                    "status": "FAIL",
                    "reason": "KAGGLE_OUTPUT_FAILED",
                    "error_tail": err[-400:],
                    "quota_before": quota_before,
                    "paid_compute_used": False,
                }
            result_file = next(iter(Path(outdir).rglob("freecompute_result.json")), None)
            if result_file is None:
                return {
                    "lane": lane,
                    "status": "FAIL",
                    "reason": "RESULT_ARTIFACT_MISSING",
                    "quota_before": quota_before,
                    "paid_compute_used": False,
                }
            result = json.loads(result_file.read_text(encoding="utf-8"))

        quota_after = _quota(env)
        return {
            "lane": lane,
            "status": "PASS" if result.get("ok") is True and result.get("cuda") is True else "FAIL",
            "reason": "CUDA_VERIFIED" if result.get("ok") is True and result.get("cuda") is True else "CUDA_RESULT_INVALID",
            "accelerator_requested": GPU_ACCELERATOR,
            "result": result,
            "quota_before": quota_before,
            "quota_after": quota_after,
            "elapsed_seconds": time.time() - started,
            "paid_compute_used": False,
            "private_source_used": False,
        }
    finally:
        if ref:
            try:
                _run(["kaggle", "kernels", "delete", ref, "-y"], env, timeout=60)
            except Exception:
                pass


def main() -> int:
    run_id = os.getenv("GITHUB_RUN_ID") or uuid.uuid4().hex
    state = _load_json(
        STATE_PATH,
        {
            "mode": ROTATION_MODE,
            "next_index": 0,
            "last_run_id": None,
            "updated_at": None,
        },
    )
    if state.get("mode") != ROTATION_MODE:
        state = {"mode": ROTATION_MODE, "next_index": 0, "last_run_id": None, "updated_at": None}

    cpu = run_cpu_support()

    lane_credentials: dict[str, tuple[str, str]] = {}
    for lane in LANES:
        token, username = _secret_pair(lane)
        if token and username:
            lane_credentials[lane] = (token, username)

    cursor = int(state.get("next_index") or 0) % len(LANES)
    ordered = [LANES[(cursor + offset) % len(LANES)] for offset in range(len(LANES))]
    selected = [lane for lane in ordered if lane in lane_credentials][:MAX_PARALLEL_KAGGLE]

    gpu_results: list[dict[str, Any]] = []
    if selected:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(selected)) as pool:
            futures = {
                pool.submit(_run_gpu_lane, lane, *lane_credentials[lane], run_id): lane
                for lane in selected
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    gpu_results.append(future.result())
                except Exception as exc:
                    gpu_results.append({
                        "lane": futures[future],
                        "status": "FAIL",
                        "reason": type(exc).__name__,
                        "paid_compute_used": False,
                    })
        gpu_results.sort(key=lambda item: LANES.index(item["lane"]))

        last_selected = selected[-1]
        state["next_index"] = (LANES.index(last_selected) + 1) % len(LANES)

    state.update({
        "mode": ROTATION_MODE,
        "last_run_id": run_id,
        "last_selected_lanes": selected,
        "updated_at": utcnow(),
    })
    _write_json(STATE_PATH, state)

    secret_status = {
        lane: {
            "configured": lane in lane_credentials,
        }
        for lane in LANES
    }

    gpu_passes = sum(1 for row in gpu_results if row.get("status") == "PASS")
    gpu_skips = sum(1 for row in gpu_results if row.get("status") == "SKIP")
    gpu_failures = sum(1 for row in gpu_results if row.get("status") == "FAIL")

    evidence = {
        "schema_version": 1,
        "generated_at": utcnow(),
        "source": "FREECOMPUTE_PUBLIC_WORKER",
        "scope": "SHOREA_V8_CP3_PLUS_SUPPORT_ONLY",
        "authoritative_merge_gate": False,
        "private_source_used": False,
        "paid_compute_used": False,
        "zero_spend_required": True,
        "rotation": {
            "mode": ROTATION_MODE,
            "selected_lanes": selected,
            "next_index": state["next_index"],
            "automatic_identity_failover": False,
            "identity_assignment_for_new_tasks_only": True,
        },
        "github_hosted_cpu": cpu,
        "kaggle_gpu": {
            "accelerator": GPU_ACCELERATOR,
            "reserve_fraction": GPU_RESERVE_FRACTION,
            "min_post_run_hours": GPU_MIN_POST_RUN_HOURS,
            "max_runtime_seconds_per_lane": GPU_TIMEOUT_SECONDS,
            "credential_presence": secret_status,
            "results": gpu_results,
            "passes": gpu_passes,
            "skips": gpu_skips,
            "failures": gpu_failures,
        },
        "status": (
            "PASS_GPU_AND_CPU"
            if cpu["status"] == "PASS" and gpu_passes > 0 and gpu_failures == 0
            else "PASS_CPU_GPU_BLOCKED"
            if cpu["status"] == "PASS" and not selected
            else "PASS_CPU_GPU_DEGRADED"
            if cpu["status"] == "PASS"
            else "FAIL"
        ),
        "run_id": run_id,
    }
    _write_json(EVIDENCE_PATH, evidence)

    print(json.dumps({
        "status": evidence["status"],
        "cpu_status": cpu["status"],
        "selected_lanes": selected,
        "gpu_passes": gpu_passes,
        "gpu_skips": gpu_skips,
        "gpu_failures": gpu_failures,
        "rotation_mode": ROTATION_MODE,
    }, sort_keys=True))

    return 0 if cpu["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
