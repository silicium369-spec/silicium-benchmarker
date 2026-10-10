#!/usr/bin/env python3
"""
==============================================================================
SILICIUM AWS IN-SITU REPRODUCTION RUNNER (AMD EPYC Milan / c6a.4xlarge)
1-Command Automated Cloud Verification & Metrology
License: PolyForm Noncommercial 1.0.0 | Patent Pending. All rights reserved.
==============================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
    HAS_BOTO3 = True
except ImportError:
    print("📦 Required library 'boto3' not found. Installing automatically...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "boto3", "--quiet"])
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError
    HAS_BOTO3 = True


def load_env_if_present() -> None:
    """Loads environment variables from .env if present in current or parent directories."""
    search_dirs = [Path.cwd(), Path(__file__).resolve().parent, *Path.cwd().parents]
    seen = set()
    for d in search_dirs:
        if d in seen or not d.exists():
            continue
        seen.add(d)
        env_file = d / ".env"
        if env_file.exists():
            try:
                with open(env_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k not in os.environ and (k.startswith("AWS_") or k.startswith("GITHUB_") or k.startswith("GH_")):
                                os.environ[k] = v
                break
            except Exception:
                pass

load_env_if_present()

DEFAULT_INSTANCE_TYPE = "c6a.4xlarge"
DEFAULT_REGION = "us-east-1"
DEFAULT_AMI = "ami-0045d7fc2ad003464"  # Ubuntu 24.04 Noble x86_64 us-east-1


def resolve_target_binary(repo_dir: Path, requested_binary: Optional[str] = None) -> Path:
    """Resolves or auto-downloads the Silicium server binary (Decoupled Zero-Binary Pattern)."""
    if requested_binary:
        p = Path(requested_binary).resolve()
        if p.exists():
            return p
        raise FileNotFoundError(f"Requested binary not found: {p}")

    # 1. Local bin/ directory (untracked in git)
    local_bin = repo_dir / "bin" / "silicium_server"
    if local_bin.exists():
        return local_bin

    # 2. Local dist/ directory
    dist_bin = repo_dir / "dist" / "silicium_server"
    if dist_bin.exists():
        return dist_bin

    # 3. User cache directory
    cache_bin = Path.home() / ".cache" / "silicium" / "silicium_server"
    if cache_bin.exists():
        return cache_bin

    # 4. Attempt auto-download from GitHub Release via gh CLI if authenticated
    release_tag = "v1.0.0-aws-eval"
    archive_name = "silicium-benchmarker-v1.0.0-x86_64-linux.tar.gz"
    dest_dir = repo_dir / "bin"
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_path = dest_dir / archive_name

    print("📥 Local binary not found. Attempting download from GitHub Release v1.0.0-aws-eval...")
    try:
        import subprocess
        res = subprocess.run(
            ["gh", "release", "download", release_tag, "-p", archive_name, "-D", str(dest_dir), "--clobber"],
            capture_output=True, text=True
        )
        if res.returncode == 0 and archive_path.exists():
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=dest_dir)
            if local_bin.exists():
                local_bin.chmod(0o755)
                print(f"✓ Binary successfully fetched from release: {local_bin}")
                return local_bin
    except Exception:
        pass

    # 5. Fallback: Download via GitHub API using GITHUB_TOKEN or GH_TOKEN if present
    gh_token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if gh_token:
        try:
            import urllib.request
            api_url = f"https://api.github.com/repos/silicium369-spec/silicium-benchmarker/releases/tags/{release_tag}"
            req = urllib.request.Request(api_url, headers={
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json"
            })
            with urllib.request.urlopen(req) as resp:
                rel_data = json.loads(resp.read().decode())
                for asset in rel_data.get("assets", []):
                    if asset.get("name") == archive_name:
                        download_req = urllib.request.Request(asset.get("url"), headers={
                            "Authorization": f"Bearer {gh_token}",
                            "Accept": "application/octet-stream"
                        })
                        with urllib.request.urlopen(download_req) as a_resp, open(archive_path, "wb") as f_out:
                            f_out.write(a_resp.read())
                        with tarfile.open(archive_path, "r:gz") as tar:
                            tar.extractall(path=dest_dir)
                        if local_bin.exists():
                            local_bin.chmod(0o755)
                            print(f"✓ Binary successfully fetched via GitHub API: {local_bin}")
                            return local_bin
        except Exception:
            pass

    raise FileNotFoundError(
        "Silicium server binary not found!\n\n"
        "To evaluate Silicium, please use one of the following methods:\n"
        "  1. Login via GitHub CLI: gh auth login\n"
        "  2. Or set your GitHub token: export GITHUB_TOKEN='<your_personal_token>'\n"
        "  3. Or manually download 'silicium-benchmarker-v1.0.0-x86_64-linux.tar.gz' from:\n"
        "     https://github.com/silicium369-spec/silicium-benchmarker/releases/tag/v1.0.0-aws-eval\n"
        "     and extract 'silicium_server' into 'bin/' (or run with --binary <path>)."
    )


def package_payload(target_binary: Path) -> Path:
    """Packages the stripped binary into a local archive."""
    if not target_binary.exists():
        raise FileNotFoundError(f"Binary not found: {target_binary}")
    
    tar_path = Path(tempfile.gettempdir()) / "silicium_payload.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(target_binary, arcname="silicium_server")
    
    print(f"📦 [1/5] Payload packaged: {tar_path} ({tar_path.stat().st_size / 1024:.1f} KB)")
    return tar_path


def resolve_or_create_telemetry_bucket(s3: Any, sts: Any, region: str, requested_bucket: Optional[str]) -> str:
    """Finds or creates an S3 bucket for telemetry transfer."""
    if requested_bucket:
        return requested_bucket
    
    env_bucket = os.getenv("AWS_BENCHMARK_BUCKET")
    if env_bucket:
        return env_bucket

    account_id = sts.get_caller_identity()["Account"]
    default_bucket_name = f"silicium-benchmark-telemetry-{account_id}"

    try:
        s3.head_bucket(Bucket=default_bucket_name)
        return default_bucket_name
    except Exception:
        pass

    try:
        print(f"🪣 Creating telemetry transfer bucket: {default_bucket_name}...")
        if region == "us-east-1":
            s3.create_bucket(Bucket=default_bucket_name)
        else:
            s3.create_bucket(
                Bucket=default_bucket_name,
                CreateBucketConfiguration={"LocationConstraint": region}
            )
        return default_bucket_name
    except Exception as e:
        buckets = s3.list_buckets().get("Buckets", [])
        if buckets:
            return buckets[0]["Name"]
        raise RuntimeError(f"Unable to resolve S3 bucket: {e}")


def generate_cloud_init_user_data(
    presigned_payload_get: str,
    presigned_results_put: str,
    presigned_heartbeat_put: str,
) -> str:
    """Generates the cloud-init benchmark execution script."""
    return f"""#!/usr/bin/env bash
set -euo pipefail

# 1. Dead Man's Switch (10 minutes max)
shutdown -h +10 "Auto-Termination Watchdog"

send_status() {{
    local msg="$1"
    curl -sf -X PUT -H "Content-Type: text/plain" -d "$msg" "{presigned_heartbeat_put}" || true
}}

send_status "PHASE_1_BOOTSTRAP"

# 2. Kernel TCP & file descriptor limits
sysctl -w net.core.somaxconn=65535 >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_max_syn_backlog=65535 >/dev/null 2>&1 || true
sysctl -w net.ipv4.ip_local_port_range="1024 65535" >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_tw_reuse=1 >/dev/null 2>&1 || true
sysctl -w net.ipv4.tcp_fin_timeout=15 >/dev/null 2>&1 || true
ulimit -n 1048576 || true

export DEBIAN_FRONTEND=noninteractive
apt-get update -y >/dev/null 2>&1
apt-get install -y --no-install-recommends wrk curl jq numactl util-linux >/dev/null 2>&1

mkdir -p /app
curl -fsSL -o /tmp/payload.tar.gz "{presigned_payload_get}"
tar -xzf /tmp/payload.tar.gz -C /app/
chmod +x /app/silicium_server

send_status "PHASE_2_STARTING_SERVER"

# Dynamic CPU partitioning (50% Server, 50% Client wrk)
NUM_CPUS=$(nproc)
if [ "$NUM_CPUS" -ge 16 ]; then
    SERVER_CORES="0-7"
    CLIENT_CORES="8-15"
    CLIENT_THREADS=8
elif [ "$NUM_CPUS" -ge 8 ]; then
    SERVER_CORES="0-3"
    CLIENT_CORES="4-7"
    CLIENT_THREADS=4
elif [ "$NUM_CPUS" -ge 4 ]; then
    SERVER_CORES="0-1"
    CLIENT_CORES="2-3"
    CLIENT_THREADS=2
else
    SERVER_CORES="0"
    CLIENT_CORES="0"
    CLIENT_THREADS=1
fi

taskset -c "$SERVER_CORES" /app/silicium_server > /tmp/server.log 2>&1 &
SERVER_PID=$!

for i in $(seq 1 100); do
    if curl -s http://127.0.0.1:3000/ > /dev/null 2>&1; then
        break
    fi
    sleep 0.05
done

send_status "PHASE_3_WARMUP"
taskset -c "$CLIENT_CORES" wrk -t "$CLIENT_THREADS" -c 64 -d 5s http://127.0.0.1:3000/ > /dev/null 2>&1

send_status "PHASE_4_BENCHMARKING_GET_64"
taskset -c "$CLIENT_CORES" wrk -t "$CLIENT_THREADS" -c 64 -d 10s --latency http://127.0.0.1:3000/ > /tmp/wrk_get_64.txt

send_status "PHASE_4_BENCHMARKING_GET_256"
taskset -c "$CLIENT_CORES" wrk -t "$CLIENT_THREADS" -c 256 -d 10s --latency http://127.0.0.1:3000/ > /tmp/wrk_get_256.txt

send_status "PHASE_4_BENCHMARKING_GET_512"
taskset -c "$CLIENT_CORES" wrk -t "$CLIENT_THREADS" -c 512 -d 10s --latency http://127.0.0.1:3000/ > /tmp/wrk_get_512.txt

send_status "PHASE_4_BENCHMARKING_USER_ID"
taskset -c "$CLIENT_CORES" wrk -t "$CLIENT_THREADS" -c 256 -d 10s --latency http://127.0.0.1:3000/user/1337 > /tmp/wrk_user_id.txt

send_status "PHASE_4_BENCHMARKING_POST_USER"
cat << 'LUAEOF' > /tmp/post.lua
wrk.method = "POST"
wrk.body   = "benchmark_user_payload_data_42"
wrk.headers["Content-Type"] = "application/x-www-form-urlencoded"
LUAEOF
taskset -c "$CLIENT_CORES" wrk -t "$CLIENT_THREADS" -c 256 -d 10s --latency -s /tmp/post.lua http://127.0.0.1:3000/user > /tmp/wrk_post_user.txt

kill $SERVER_PID 2>/dev/null || true

send_status "PHASE_5_AGGREGATING_RESULTS"

python3 - << 'PYEOF'
import json, re

def parse_wrk(file_path):
    try:
        with open(file_path, "r") as f:
            content = f.read()
    except Exception:
        return {{"requests_per_sec": 0.0, "p50_latency": "N/A", "p90_latency": "N/A", "p99_latency": "N/A"}}
    
    rps_m = re.search(r"Requests/sec:\\s+([\\d\\.]+)", content)
    rps = float(rps_m.group(1)) if rps_m else 0.0
    
    p50_m = re.search(r"50%\\s+([\\d\\.]+[a-zµs]+)", content)
    p75_m = re.search(r"75%\\s+([\\d\\.]+[a-zµs]+)", content)
    p90_m = re.search(r"90%\\s+([\\d\\.]+[a-zµs]+)", content)
    p99_m = re.search(r"99%\\s+([\\d\\.]+[a-zµs]+)", content)
    
    return {{
        "requests_per_sec": rps,
        "p50_latency": p50_m.group(1) if p50_m else "N/A",
        "p75_latency": p75_m.group(1) if p75_m else "N/A",
        "p90_latency": p90_m.group(1) if p90_m else "N/A",
        "p99_latency": p99_m.group(1) if p99_m else "N/A",
        "raw": content.strip()
    }}

cpu_info = ""
try:
    with open("/proc/cpuinfo") as f:
        for line in f:
            if "model name" in line:
                cpu_info = line.split(":", 1)[1].strip()
                break
except Exception:
    cpu_info = "AMD EPYC Processor"

results = {{
    "instance_type": "{DEFAULT_INSTANCE_TYPE}",
    "cpu_model": cpu_info,
    "get_root_64_conns": parse_wrk("/tmp/wrk_get_64.txt"),
    "get_root_256_conns": parse_wrk("/tmp/wrk_get_256.txt"),
    "get_root_512_conns": parse_wrk("/tmp/wrk_get_512.txt"),
    "get_user_id_256_conns": parse_wrk("/tmp/wrk_user_id.txt"),
    "post_user_256_conns": parse_wrk("/tmp/wrk_post_user.txt")
}}

with open("/tmp/results.json", "w") as f:
    json.dump(results, f, indent=2)
PYEOF

curl -sf -X PUT -H "Content-Type: application/json" -T /tmp/results.json "{presigned_results_put}" || true

send_status "PHASE_6_DONE"
poweroff
"""


def run_aws_calibration(
    instance_type: str = DEFAULT_INSTANCE_TYPE,
    region: str = DEFAULT_REGION,
    bucket: Optional[str] = None,
    binary: Optional[str] = None,
    max_wait_seconds: int = 480,
) -> Dict[str, Any]:
    """Orchestrates ephemeral Spot instance benchmark."""
    if not HAS_BOTO3:
        raise ImportError("Boto3 is required: pip install boto3")

    repo_dir = Path(__file__).resolve().parent
    target_binary = resolve_target_binary(repo_dir, requested_binary=binary)

    tar_path = package_payload(target_binary)

    session = boto3.Session(region_name=region)
    s3 = session.client("s3")
    sts = session.client("sts")
    ec2 = session.client("ec2")

    try:
        sts.get_caller_identity()
    except Exception as e:
        print("\n❌ AWS CREDENTIALS ERROR:")
        print("   Unable to authenticate with AWS. Please configure your AWS credentials:")
        print("   1. Run 'aws configure' (standard AWS CLI)")
        print("   2. Or set environment variables:")
        print("        export AWS_ACCESS_KEY_ID='your-access-key'")
        print("        export " + "AWS_SECRET_" + "ACCESS_KEY='your-secret-key'")
        print("        export AWS_DEFAULT_REGION='us-east-1'")
        print("   3. Or provide a '.env' file with your AWS credentials.\n")
        raise SystemExit(1)

    target_bucket = resolve_or_create_telemetry_bucket(s3, sts, region, bucket)
    run_id = int(time.time())
    payload_key = f"benchmarks/payload_{run_id}.tar.gz"
    results_key = f"benchmarks/results_{run_id}.json"
    heartbeat_key = f"benchmarks/heartbeat_{run_id}.txt"

    print(f"☁️  [2/5] Staging payload to S3 (s3://{target_bucket}/{payload_key})...")
    s3.upload_file(str(tar_path), target_bucket, payload_key)

    presigned_payload_get = s3.generate_presigned_url(
        "get_object", Params={"Bucket": target_bucket, "Key": payload_key}, ExpiresIn=1800
    )
    presigned_results_put = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": target_bucket, "Key": results_key, "ContentType": "application/json"},
        ExpiresIn=1800,
    )
    presigned_heartbeat_put = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": target_bucket, "Key": heartbeat_key, "ContentType": "text/plain"},
        ExpiresIn=1800,
    )

    user_data_script = generate_cloud_init_user_data(
        presigned_payload_get, presigned_results_put, presigned_heartbeat_put
    )

    print(f"🚀 [3/5] Allocating Spot {instance_type} on AWS {region}...")
    subnets_res = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])
    candidate_subnets = [
        s for s in subnets_res.get("Subnets", [])
        if s.get("AvailabilityZone") != f"{region}e"
    ]
    if not candidate_subnets:
        candidate_subnets = subnets_res.get("Subnets", [])

    launch_kwargs: Dict[str, Any] = {
        "ImageId": DEFAULT_AMI,
        "InstanceType": instance_type,
        "MinCount": 1,
        "MaxCount": 1,
        "UserData": user_data_script,
        "InstanceMarketOptions": {
            "MarketType": "spot",
            "SpotOptions": {"SpotInstanceType": "one-time"},
        },
        "BlockDeviceMappings": [
            {
                "DeviceName": "/dev/sda1",
                "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ],
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "ManagedBy", "Value": "Silicium"},
                    {"Key": "Name", "Value": f"silicium-benchmark-{instance_type}"},
                ],
            }
        ],
    }

    instance_id = None
    last_err = None
    for sub in candidate_subnets:
        az = sub.get("AvailabilityZone", "unknown")
        sid = sub["SubnetId"]
        launch_kwargs["SubnetId"] = sid
        try:
            print(f"   Attempting allocation in AZ {az} ({sid})...")
            run_res = ec2.run_instances(**launch_kwargs)
            instance_id = run_res["Instances"][0]["InstanceId"]
            print(f"   ✓ Instance allocated: {instance_id} [{instance_type}] in {az}")
            break
        except ClientError as e:
            last_err = e
            print(f"   ⚠️ AZ {az} unavailable ({e.response['Error']['Code']}), trying next AZ...")
            continue

    if not instance_id:
        raise RuntimeError(f"Failed to allocate Spot instance across subnets: {last_err}")

    results_data: Optional[Dict[str, Any]] = None
    start_time = time.time()
    last_hb = ""

    try:
        print(f"⏱️  [4/5] Monitoring in-situ benchmark run (timeout: {max_wait_seconds}s)...")
        while time.time() - start_time < max_wait_seconds:
            elapsed = int(time.time() - start_time)
            try:
                hb_obj = s3.get_object(Bucket=target_bucket, Key=heartbeat_key)
                hb_text = hb_obj["Body"].read().decode("utf-8").strip()
                if hb_text != last_hb:
                    print(f"   📡 [T+{elapsed:>3}s] EC2 Status: {hb_text}")
                    last_hb = hb_text
            except Exception:
                pass

            try:
                res_obj = s3.get_object(Bucket=target_bucket, Key=results_key)
                results_data = json.loads(res_obj["Body"].read().decode("utf-8"))
                print(f"🎉 [T+{elapsed:>3}s] Benchmark results received successfully!")
                break
            except Exception:
                if last_hb == "PHASE_6_DONE":
                    time.sleep(2)
                    try:
                        res_obj = s3.get_object(Bucket=target_bucket, Key=results_key)
                        results_data = json.loads(res_obj["Body"].read().decode("utf-8"))
                        print(f"🎉 [T+{elapsed:>3}s] Benchmark results received successfully!")
                        break
                    except Exception:
                        pass

            time.sleep(3)

        if not results_data:
            raise TimeoutError("Benchmark did not return results within timeout.")

    finally:
        print(f"🛑 [5/5] Terminating Spot instance {instance_id}...")
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
            print(f"   ✓ Instance {instance_id} terminated (0 orphan resources).")
        except Exception as e:
            print(f"   ⚠️ Termination error: {e}")

    out_dir = repo_dir / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"aws_{instance_type}_benchmark.json"
    with open(out_file, "w") as f:
        json.dump(results_data, f, indent=2)
    print(f"💾 Results saved locally: {out_file}")

    return results_data


def print_executive_summary(results: Dict[str, Any]) -> None:
    """Prints institutional comparison table."""
    print("\n" + "=" * 80)
    print(f"🏆 CERTIFIED SILICIUM BENCHMARK RESULTS ON AWS {results.get('instance_type')} (AMD EPYC)")
    print(f"   Host CPU: {results.get('cpu_model')}")
    print("=" * 80)
    print(f"{'Workload Scenario':<32} | {'Throughput (req/s)':<18} | {'P50 Latency':<12} | {'P99 Latency':<12}")
    print("-" * 80)

    g64 = results.get("get_root_64_conns", {})
    print(f"{'GET / (64 conns)':<32} | {g64.get('requests_per_sec', 0.0):>15,.0f} rps | {g64.get('p50_latency', 'N/A'):<12} | {g64.get('p99_latency', 'N/A'):<12}")

    g256 = results.get("get_root_256_conns", {})
    print(f"{'GET / (256 conns)':<32} | {g256.get('requests_per_sec', 0.0):>15,.0f} rps | {g256.get('p50_latency', 'N/A'):<12} | {g256.get('p99_latency', 'N/A'):<12}")

    g512 = results.get("get_root_512_conns", {})
    print(f"{'GET / (512 conns - Peak)':<32} | {g512.get('requests_per_sec', 0.0):>15,.0f} rps | {g512.get('p50_latency', 'N/A'):<12} | {g512.get('p99_latency', 'N/A'):<12}")

    uid = results.get("get_user_id_256_conns", {})
    print(f"{'GET /user/:id (256 conns)':<32} | {uid.get('requests_per_sec', 0.0):>15,.0f} rps | {uid.get('p50_latency', 'N/A'):<12} | {uid.get('p99_latency', 'N/A'):<12}")

    post = results.get("post_user_256_conns", {})
    print(f"{'POST /user (256 conns)':<32} | {post.get('requests_per_sec', 0.0):>15,.0f} rps | {post.get('p50_latency', 'N/A'):<12} | {post.get('p99_latency', 'N/A'):<12}")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Silicium 1-Command AWS Benchmark Runner")
    parser.add_argument("--instance-type", default=DEFAULT_INSTANCE_TYPE, help="Instance type (default: c6a.4xlarge)")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS Region (default: us-east-1)")
    parser.add_argument("--bucket", default=None, help="S3 bucket for telemetry staging")
    parser.add_argument("--binary", default=None, help="Path to local silicium_server binary (optional)")
    args = parser.parse_args()

    results = run_aws_calibration(
        instance_type=args.instance_type,
        region=args.region,
        bucket=args.bucket,
        binary=args.binary,
    )
    print_executive_summary(results)
