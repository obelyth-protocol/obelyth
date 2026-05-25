"""
Obelyth Miner Daemon
========================
Runs on the GPU provider's machine.
Registers hardware, accepts compute jobs, runs inference/fine-tuning,
submits results with verification hash, earns OBY rewards.

Usage:
  python -m compute.miner \
    --wallet-address OBY... \
    --node http://seed.obelyth.io:8334 \
    --stake 5000
"""

import os
import sys
import json
import time
import hashlib
import logging
import argparse
import threading
import subprocess
import platform
import urllib.request
from typing import Optional

log = logging.getLogger('obelyth.miner')


# ── GPU Detection ──────────────────────────────────────────────────────────────

def detect_gpus() -> list[dict]:
    """
    Detect available GPUs via nvidia-smi.
    Returns list of GPU info dicts.
    Falls back to CPU-only mode if no NVIDIA GPUs found.
    """
    gpus = []
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total,driver_version',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            for i, line in enumerate(result.stdout.strip().split('\n')):
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 2:
                    gpus.append({
                        'index'        : i,
                        'name'         : parts[0],
                        'vram_mb'      : int(parts[1]) if parts[1].isdigit() else 0,
                        'driver'       : parts[2] if len(parts) > 2 else 'unknown',
                    })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Try AMD ROCm
    if not gpus:
        try:
            result = subprocess.run(
                ['rocm-smi', '--showproductname'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                for i, line in enumerate(result.stdout.strip().split('\n')):
                    if 'GPU' in line:
                        gpus.append({'index': i, 'name': line.strip(),
                                     'vram_mb': 16384, 'driver': 'rocm'})
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    if not gpus:
        # CPU fallback — can still run small inference
        gpus.append({
            'index' : 0,
            'name'  : f'CPU ({platform.processor() or "unknown"})',
            'vram_mb': 0,
            'driver': 'cpu',
        })

    return gpus


def measure_bandwidth() -> float:
    """Estimate network bandwidth in Gbps (rough)."""
    try:
        import socket
        # Measure loopback as a floor; real measurement needs iperf3
        return 1.0
    except Exception:
        return 0.1


def run_benchmark(gpu_index: int = 0) -> dict:
    """
    Run a synthetic benchmark to prove hardware capability.
    In production: runs a standardized matrix multiply + small inference pass.
    Returns benchmark metrics signed with miner key.
    """
    start = time.time()
    # Synthetic: SHA3 throughput as proxy for compute
    count = 0
    deadline = start + 5.0
    data = b'obelyth-benchmark-' * 100
    while time.time() < deadline:
        hashlib.sha3_256(data + count.to_bytes(4, 'big')).digest()
        count += 1
    elapsed = time.time() - start
    throughput = count / elapsed

    return {
        'throughput_hash_per_sec': int(throughput),
        'duration_sec'           : round(elapsed, 2),
        'timestamp'              : int(time.time()),
        'score'                  : min(100.0, throughput / 10_000),
    }


# ── Job Runner ────────────────────────────────────────────────────────────────

class JobRunner:
    """
    Executes assigned compute jobs.
    Inference: loads model via subprocess (isolates GPU memory).
    Fine-tuning: runs training script with provided config.
    """

    def run_inference(
        self,
        model_id : str,
        inputs   : list,
        task     : str,
        params   : dict,
        seed     : int = 0,
    ) -> dict:
        """
        Run inference. In production: calls local model server
        (vLLM, TGI, or ollama) via localhost API.
        Returns result dict with output text + verification hash.

        The `seed` argument is propagated to the inference framework so
        the rerun on a challenger node produces the same output hash.
        Without seed pinning, GPU driver/kernel/quantization variance
        causes legitimate runs to look like faults.
        """
        log.info(
            f"Running inference: {task} model={model_id} "
            f"inputs={len(inputs)} seed={seed}"
        )
        start = time.time()

        # Production: call vLLM/TGI endpoint with seed in SamplingParams
        # result = self._call_local_server(model_id, inputs, task, params, seed)

        # Stub for development. Note: we incorporate the seed into the stub
        # output so the result_hash is deterministic per (inputs, seed). This
        # mirrors what real vLLM produces when seed is pinned.
        outputs = []
        for inp in inputs:
            stub_text = (
                f"[Obelyth inference stub] "
                f"Model '{model_id}' (seed={seed}) processed: '{str(inp)[:60]}'"
            )
            outputs.append({'generated_text': stub_text, 'score': 0.99})

        elapsed = time.time() - start
        # Canonical JSON: sorted keys, compact separators — same convention
        # the compute/api.py uses for input_payload_hash derivation, so the
        # challenger gets a byte-identical hash on rerun.
        result_hash = hashlib.sha256(
            json.dumps(outputs, sort_keys=True,
                       separators=(',', ':')).encode()
        ).hexdigest()

        return {
            'outputs'     : outputs,
            'latency_ms'  : round(elapsed * 1000, 1),
            'result_hash' : result_hash,
            'model_id'    : model_id,
            'seed'        : seed,
        }

    def run_fine_tuning(self, config: dict) -> dict:
        """
        Run fine-tuning job.
        In production: launches subprocess running:
          accelerate launch train_qlora.py --config config.json
        Returns result CID (IPFS hash of output weights).
        """
        model_id   = config.get('base_model', 'unknown')
        method     = config.get('method', 'qlora')
        epochs     = config.get('epochs', 3)
        dataset_cid= config.get('dataset_cid', '')

        log.info(f"Fine-tuning: {method} {model_id} for {epochs} epochs "
                 f"dataset={dataset_cid[:16]}")

        # Production:
        # subprocess.run(['accelerate', 'launch', 'train_qlora.py',
        #                 '--config', json_config_path])
        # Then upload weights to IPFS, get CID

        # Stub: simulate training time
        time.sleep(2)
        output_hash = hashlib.sha3_256(
            (model_id + dataset_cid + str(epochs)).encode()
        ).hexdigest()
        result_cid  = 'Qm' + output_hash[:44]

        return {
            'result_cid'       : result_cid,
            'verification_hash': output_hash,
            'epochs_completed' : epochs,
            'model_id'         : model_id,
        }

    def _call_local_server(self, model_id: str, inputs: list, task: str, params: dict) -> list:
        """Call local vLLM / TGI server."""
        # vLLM OpenAI-compatible endpoint
        body = json.dumps({
            'model' : model_id,
            'prompt': inputs[0] if inputs else '',
            'max_tokens': params.get('max_new_tokens', 256),
        }).encode()
        req = urllib.request.Request(
            'http://localhost:8000/v1/completions',
            data=body,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        return [{'generated_text': c['text']} for c in resp.get('choices', [])]


# ── Miner Daemon ──────────────────────────────────────────────────────────────

class MinerDaemon:
    """
    Main miner process.
    Registers with node, polls for jobs, executes them, earns rewards.
    """

    POLL_INTERVAL  = 5      # seconds between job polls
    HEARTBEAT_SECS = 60     # uptime heartbeat interval

    def __init__(
        self,
        wallet_address : str,
        node_url       : str,
        stake_oby      : float,
        region         : str  = 'auto',
    ):
        self.address   = wallet_address
        self.node_url  = node_url.rstrip('/')
        self.stake_oby = stake_oby
        self.region    = region or self._detect_region()
        self.runner    = JobRunner()
        self._running  = False
        self._earnings = 0.0   # OBY earned this session

    def start(self):
        log.info("=== Obelyth Miner Starting ===")
        gpus = detect_gpus()
        for g in gpus:
            log.info(f"  GPU: {g['name']}  VRAM: {g.get('vram_mb',0)//1024}GB")

        log.info("Running benchmark...")
        bench = run_benchmark()
        log.info(f"  Score: {bench['score']:.1f}/100  "
                 f"Throughput: {bench['throughput_hash_per_sec']:,}/s")

        # Register with node
        self._register(gpus, bench)

        self._running = True
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        self._job_loop()

    def _register(self, gpus: list, bench: dict):
        gpu = gpus[0] if gpus else {}
        payload = {
            'address'      : self.address,
            'gpu_model'    : gpu.get('name', 'unknown'),
            'gpu_count'    : len([g for g in gpus if g.get('driver') != 'cpu']),
            'vram_gb'      : gpu.get('vram_mb', 0) // 1024,
            'bandwidth_gbps': measure_bandwidth(),
            'region'       : self.region,
            'stake_oby'    : self.stake_oby,
            'benchmark'    : bench,
        }
        resp = self._post('/compute/register', payload)
        if resp:
            log.info(f"Registered with node. Miner ID: {self.address[:16]}...")
        else:
            log.warning("Could not reach node — operating in offline mode")

    def _job_loop(self):
        log.info("Polling for jobs...")
        while self._running:
            try:
                job = self._get('/compute/nextjob' +
                                f'?address={self.address}')
                if job and job.get('job_id'):
                    self._execute_job(job)
                else:
                    time.sleep(self.POLL_INTERVAL)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Job loop error: {e}")
                time.sleep(10)

        log.info(f"Miner stopped. Total earned: {self._earnings:.4f} OBY")

    # ── Determinism enforcement ──────────────────────────────────────────────
    # The job envelope carries pinned model_hash, container_digest, and seed.
    # The miner must respect these so the challenger's rerun is reproducible.
    # Phase 3.2 validates the envelope format; Phase 5 will add real container
    # pulling and model-weight verification.

    _HEX64 = frozenset('0123456789abcdef')

    @staticmethod
    def _is_sha256_hex(s: str) -> bool:
        return (isinstance(s, str) and len(s) == 64
                and all(c in MinerDaemon._HEX64 for c in s.lower()))

    @staticmethod
    def _is_oci_digest(s: str) -> bool:
        return (isinstance(s, str) and s.startswith('sha256:')
                and MinerDaemon._is_sha256_hex(s[len('sha256:'):]))

    def _validate_envelope(self, job: dict) -> tuple[bool, str]:
        """
        Validate the determinism envelope before executing.
        Returns (ok, error_message). On failure, miner rejects the job
        and reports it to the node — does NOT slash itself, just signals
        the job was malformed and can't be executed reproducibly.
        """
        model_hash = job.get('model_hash', '')
        if not self._is_sha256_hex(model_hash):
            return False, f'malformed model_hash: {model_hash!r}'
        container_digest = job.get('container_digest', '')
        if not self._is_oci_digest(container_digest):
            return False, f'malformed container_digest: {container_digest!r}'
        seed = job.get('seed')
        if not isinstance(seed, int) or seed < 0 or seed >= 2**64:
            return False, f'seed must be uint64, got {seed!r}'
        return True, ''

    def _verify_model_hash(self, model_id: str, expected_hash: str) -> bool:
        """
        Phase 3.2: stub. Real impl in Phase 5 will:
          - check local cache for model_id
          - if not present, download from HuggingFace
          - compute SHA-256 of the weight tensors in canonical order
          - compare to expected_hash
        For now we log what we *would* verify and trust the envelope.
        """
        log.info(
            f"Determinism: would verify model {model_id} matches "
            f"hash {expected_hash[:12]}.. (Phase 5 real verification)"
        )
        return True

    def _pull_container(self, container_digest: str) -> bool:
        """
        Phase 3.2: stub. Real impl in Phase 5 will:
          - call `docker pull <registry>/obelyth-vllm@<digest>` or podman
          - confirm pulled image's manifest hash matches container_digest
          - if not, refuse to run
        For now we log what we *would* pull.
        """
        log.info(
            f"Determinism: would pull container {container_digest[:24]}.. "
            f"(Phase 5 real OCI pull)"
        )
        return True

    def _execute_job(self, job: dict):
        job_id   = job['job_id']
        job_type = job.get('job_type', 'inference')
        log.info(f"Job received: {job_id} type={job_type}")

        # ── Determinism envelope validation ──
        # Reject malformed envelopes BEFORE attempting any execution.
        # The verification engine already validates these at submission,
        # so this is defence-in-depth + protects against malicious nodes
        # that might forward a corrupted job to the miner.
        ok, err = self._validate_envelope(job)
        if not ok:
            log.error(f"Job {job_id} rejected: {err}")
            self._post('/compute/result', {
                'job_id'    : job_id,
                'miner_addr': self.address,
                'status'    : 'failed',
                'error'     : f'envelope validation: {err}',
            })
            return

        model_hash       = job['model_hash']
        container_digest = job['container_digest']
        seed             = job['seed']

        # Phase 3.2: stub model + container verification (Phase 5 makes real)
        if not self._verify_model_hash(job.get('model_id', ''), model_hash):
            log.error(f"Job {job_id} rejected: model hash mismatch")
            self._post('/compute/result', {
                'job_id'    : job_id,
                'miner_addr': self.address,
                'status'    : 'failed',
                'error'     : 'model hash mismatch',
            })
            return
        if not self._pull_container(container_digest):
            log.error(f"Job {job_id} rejected: container digest mismatch")
            self._post('/compute/result', {
                'job_id'    : job_id,
                'miner_addr': self.address,
                'status'    : 'failed',
                'error'     : 'container digest mismatch',
            })
            return

        try:
            if job_type in ('inference', 'embedding'):
                result = self.runner.run_inference(
                    model_id = job.get('model_id', ''),
                    inputs   = job.get('inputs', []),
                    task     = job.get('task', 'text-generation'),
                    params   = job.get('params', {}),
                    seed     = seed,
                )
            elif job_type == 'fine_tuning':
                result = self.runner.run_fine_tuning(job.get('config', {}))
            else:
                log.warning(f"Unknown job type: {job_type}")
                return

            # Submit result
            resp = self._post('/compute/result', {
                'job_id'          : job_id,
                'miner_addr'      : self.address,
                'result_cid'      : result.get('result_cid', ''),
                'result_hash'     : result.get('result_hash',
                                               result.get('verification_hash', '')),
                'outputs'         : result.get('outputs', []),
                'latency_ms'      : result.get('latency_ms', 0),
            })

            if resp:
                reward = resp.get('oby_reward', 0.0)
                self._earnings += reward
                log.info(f"Job {job_id} complete. Reward: {reward:.4f} OBY "
                         f"(total: {self._earnings:.4f} OBY)")

        except Exception as e:
            log.error(f"Job {job_id} failed: {e}")
            self._post('/compute/result', {
                'job_id'    : job_id,
                'miner_addr': self.address,
                'status'    : 'failed',
                'error'     : str(e),
            })

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(self.HEARTBEAT_SECS)
            self._post('/compute/heartbeat', {
                'address' : self.address,
                'earnings': self._earnings,
                'uptime_s': self.HEARTBEAT_SECS,
            })
            log.debug(f"Heartbeat. Earnings: {self._earnings:.4f} OBY")

    def _detect_region(self) -> str:
        try:
            with urllib.request.urlopen('https://ipinfo.io/region', timeout=3) as r:
                return r.read().decode().strip()
        except Exception:
            return 'unknown'

    def _get(self, endpoint: str) -> Optional[dict]:
        try:
            with urllib.request.urlopen(self.node_url + endpoint, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            log.debug(f"GET {endpoint}: {e}")
            return None

    def _post(self, endpoint: str, data: dict) -> Optional[dict]:
        try:
            body = json.dumps(data).encode()
            req  = urllib.request.Request(
                self.node_url + endpoint, data=body,
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:
            log.debug(f"POST {endpoint}: {e}")
            return None


def main():
    logging.basicConfig(
        level  = logging.INFO,
        format = '%(asctime)s [miner] %(levelname)s %(message)s',
        datefmt= '%H:%M:%S',
    )
    parser = argparse.ArgumentParser(description='Obelyth GPU Miner')
    parser.add_argument('--wallet-address', required=True, help='Your OBY wallet address')
    parser.add_argument('--node',           default='http://127.0.0.1:8334', help='Node URL')
    parser.add_argument('--stake',          type=float, default=1000.0, help='OBY stake amount')
    parser.add_argument('--region',         default='auto', help='Geographic region')
    args = parser.parse_args()

    daemon = MinerDaemon(
        wallet_address = args.wallet_address,
        node_url       = args.node,
        stake_oby      = args.stake,
        region         = args.region,
    )
    daemon.start()


if __name__ == '__main__':
    main()
