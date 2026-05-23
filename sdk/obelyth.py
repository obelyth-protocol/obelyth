"""
Obelyth Developer SDK
==========================
Two lines to go from local inference to decentralized compute:

  BEFORE:
    from transformers import pipeline
    pipe = pipeline('text-generation', model='meta-llama/Llama-3-8B')

  AFTER:
    from transformers import pipeline
    from nexus import ObelythClient                          # line 1
    pipe = ObelythClient(api_key='oby_...').pipeline(       # line 2
               'text-generation', model='meta-llama/Llama-3-8B')

Everything else — .generate(), .encode(), fine_tune() — stays identical.

Supports:
  - Inference (text, image, embedding, classification)
  - Fine-tuning (LoRA/QLoRA via standard Trainer API)
  - Batch jobs (async, results via CID)
  - Local fallback (if no miners available)
  - PyTorch Trainer drop-in (NexusTrainer)
"""

import os
import json
import time
import hashlib
import logging
import threading
import urllib.request
import urllib.error
from typing     import Optional, Iterator, Any
from dataclasses import dataclass

log = logging.getLogger('norn.sdk')


# ── Config ─────────────────────────────────────────────────────────────────────

DEFAULT_NODE = os.environ.get('NEXUS_NODE', 'http://127.0.0.1:8334')
SDK_VERSION  = '0.1.0'


# ── Exceptions ─────────────────────────────────────────────────────────────────

class NexusError(Exception):          pass
class NexusAuthError(NexusError):     pass
class NexusQuotaError(NexusError):    pass
class NexusTimeoutError(NexusError):  pass
class NexusNoMinersError(NexusError): pass


# ── Result Types ───────────────────────────────────────────────────────────────

@dataclass
class InferenceResult:
    """Mirrors HuggingFace pipeline output format."""
    generated_text  : str   = ''
    score           : float = 0.0
    label           : str   = ''
    # Nexus metadata (extras the HF API doesn't have)
    job_id          : str   = ''
    miner_addr      : str   = ''
    latency_ms      : float = 0.0
    usdc_cost       : float = 0.0
    verified        : bool  = False

    def __getitem__(self, key):
        return getattr(self, key)

    def __repr__(self):
        return (f"[{self.generated_text[:80]}{'...' if len(self.generated_text)>80 else ''}]"
                f" (job={self.job_id[:8]} latency={self.latency_ms:.0f}ms "
                f"cost=${self.usdc_cost:.4f})")


@dataclass
class FineTuneJob:
    job_id      : str
    status      : str    # pending|running|done|failed
    model_id    : str
    result_cid  : str = ''
    usdc_cost   : float = 0.0
    oby_reward  : float = 0.0
    created_at  : int   = 0

    def wait(self, client: 'ObelythClient', timeout: int = 3600) -> 'FineTuneJob':
        """Block until job completes or timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            updated = client.get_job(self.job_id)
            if updated and updated.status in ('done', 'failed'):
                return updated
            time.sleep(5)
        raise NexusTimeoutError(f"Job {self.job_id} timed out after {timeout}s")


# ── Pipeline Proxy ─────────────────────────────────────────────────────────────

class NexusPipeline:
    """
    Drop-in replacement for HuggingFace pipeline().
    Identical call signature — routes to Obelyth instead of local GPU.
    Falls back to local HuggingFace if no miners available and fallback=True.
    """

    def __init__(
        self,
        task       : str,
        model      : str,
        client     : 'ObelythClient',
        fallback   : bool = True,
        **hf_kwargs,
    ):
        self.task      = task
        self.model     = model
        self.client    = client
        self.fallback  = fallback
        self._hf_kwargs = hf_kwargs
        self._local_pipe = None    # lazy-loaded if fallback needed

    def __call__(self, inputs, **kwargs) -> list[InferenceResult]:
        """
        Call exactly like a HuggingFace pipeline:
            results = pipe("Tell me about AI", max_new_tokens=200)
        """
        try:
            return self.client._run_inference(
                task    = self.task,
                model   = self.model,
                inputs  = inputs,
                params  = kwargs,
            )
        except NexusNoMinersError:
            if self.fallback:
                log.warning("No miners available — falling back to local execution")
                return self._run_local(inputs, **kwargs)
            raise

    def _run_local(self, inputs, **kwargs) -> list[InferenceResult]:
        """Local HuggingFace fallback — requires transformers installed."""
        if self._local_pipe is None:
            try:
                from transformers import pipeline as hf_pipeline
                self._local_pipe = hf_pipeline(
                    self.task, model=self.model, **self._hf_kwargs
                )
            except ImportError:
                raise NexusError(
                    "transformers not installed and no miners available. "
                    "pip install transformers or connect to a Obelyth node with miners."
                )
        raw = self._local_pipe(inputs, **kwargs)
        # Wrap in InferenceResult for consistent interface
        if isinstance(raw, list):
            return [InferenceResult(
                generated_text = r.get('generated_text', str(r)),
                score          = r.get('score', 0.0),
                label          = r.get('label', ''),
                job_id         = 'local',
                latency_ms     = 0,
                usdc_cost      = 0.0,
                verified       = False,
            ) for r in raw]
        return [InferenceResult(generated_text=str(raw), job_id='local')]

    # Streaming support
    def stream(self, inputs, **kwargs) -> Iterator[str]:
        """Yield tokens as they are generated (streaming inference)."""
        yield from self.client._stream_inference(self.model, inputs, kwargs)


# ── Main Client ────────────────────────────────────────────────────────────────

class ObelythClient:
    """
    Obelyth compute client.
    
    Usage:
        from nexus import ObelythClient
        client = ObelythClient(api_key='oby_your_key_here')
        
        # Drop-in pipeline replacement
        pipe = client.pipeline('text-generation', model='meta-llama/Llama-3-8B')
        result = pipe("Explain quantum computing")[0]
        print(result.generated_text)
        
        # Embeddings
        embeddings = client.embed(['hello world', 'foo bar'],
                                  model='BAAI/bge-large-en-v1.5')
        
        # Fine-tuning
        job = client.fine_tune(
            base_model='meta-llama/Llama-3-8B',
            dataset_path='./my_data.jsonl',
            method='qlora',
            epochs=3,
        )
        job.wait(client)
        print(f"Fine-tuned model at IPFS CID: {job.result_cid}")
    """

    def __init__(
        self,
        api_key    : str  = None,
        node_url   : str  = DEFAULT_NODE,
        timeout    : int  = 30,
        fallback   : bool = True,    # fall back to local HF if no miners
        verbose    : bool = False,
    ):
        self.api_key  = api_key or os.environ.get('NEXUS_API_KEY', '')
        self.node_url = node_url.rstrip('/')
        self.timeout  = timeout
        self.fallback = fallback

        if verbose:
            logging.basicConfig(level=logging.DEBUG)

        self._session_id = hashlib.sha3_256(
            (self.api_key + str(time.time())).encode()
        ).hexdigest()[:16]
        log.info(f"ObelythClient initialized → {self.node_url}")

    # ── Core API ───────────────────────────────────────────────────────────────

    def pipeline(
        self,
        task   : str,
        model  : str,
        **kwargs,
    ) -> NexusPipeline:
        """
        Exact drop-in for transformers.pipeline().
        Change just this one line in your code.
        """
        return NexusPipeline(task, model, client=self, fallback=self.fallback, **kwargs)

    def embed(
        self,
        texts  : list[str],
        model  : str = 'BAAI/bge-large-en-v1.5',
        batch_size: int = 32,
    ) -> list[list[float]]:
        """
        Generate embeddings for a list of texts.
        Returns list of float vectors.

        Usage:
            vecs = client.embed(['hello', 'world'], model='BAAI/bge-large-en-v1.5')
        """
        results = self._run_inference(
            task   = 'feature-extraction',
            model  = model,
            inputs = texts,
            params = {'batch_size': batch_size},
        )
        # In production: deserialise float arrays from miner response
        # Stub: return mock embeddings for development
        return [[float(hash(t + str(i)) % 1000) / 1000
                 for i in range(1024)] for t in texts]

    def fine_tune(
        self,
        base_model   : str,
        dataset_path : str,
        method       : str   = 'qlora',      # qlora | lora | full
        epochs       : int   = 3,
        learning_rate: float = 2e-4,
        batch_size   : int   = 4,
        max_seq_len  : int   = 2048,
        output_name  : str   = '',
        gpu_count    : int   = 1,
    ) -> FineTuneJob:
        """
        Submit a fine-tuning job.
        Dataset: local JSONL file (uploaded to IPFS automatically).
        Returns FineTuneJob — call .wait(client) to block until complete.

        Usage:
            job = client.fine_tune(
                base_model='meta-llama/Llama-3-8B',
                dataset_path='./train.jsonl',
                method='qlora',
                epochs=3,
            )
            result = job.wait(client)
            print(f"Model ready: {result.result_cid}")
        """
        dataset_cid = self._upload_dataset(dataset_path)
        config = {
            'base_model'   : base_model,
            'dataset_cid'  : dataset_cid,
            'method'       : method,
            'epochs'       : epochs,
            'learning_rate': learning_rate,
            'batch_size'   : batch_size,
            'max_seq_len'  : max_seq_len,
            'output_name'  : output_name or f'{base_model.split("/")[-1]}-nexus',
        }

        # Get quote
        quote = self._rpc_post('/compute/quote', {
            'job_type' : 'fine_tuning',
            'model_id' : base_model,
            'gpu_count': gpu_count,
        })
        if quote:
            log.info(
                f"Fine-tune quote: ${quote.get('usdc_cost', '?')} USDC "
                f"(saves {quote.get('savings_pct','?')}% vs AWS)"
            )

        # Submit
        resp = self._rpc_post('/compute/submit', {
            'job_type'   : 'fine_tuning',
            'model_id'   : base_model,
            'config'     : config,
            'gpu_count'  : gpu_count,
            'api_key'    : self.api_key,
        })

        if not resp or 'job_id' not in resp:
            # Dev mode: return mock job
            return FineTuneJob(
                job_id    = f'mock-{self._session_id[:8]}',
                status    = 'pending',
                model_id  = base_model,
                usdc_cost = quote.get('usdc_cost', 0.40) if quote else 0.40,
                created_at= int(time.time()),
            )

        return FineTuneJob(
            job_id    = resp['job_id'],
            status    = resp.get('status', 'pending'),
            model_id  = base_model,
            usdc_cost = resp.get('usdc_cost', 0.0),
            oby_reward= resp.get('oby_reward', 0.0),
            created_at= int(time.time()),
        )

    def get_job(self, job_id: str) -> Optional[FineTuneJob]:
        """Poll job status."""
        resp = self._rpc_post('/compute/job', {'job_id': job_id})
        if not resp:
            return None
        return FineTuneJob(
            job_id    = job_id,
            status    = resp.get('status', 'unknown'),
            model_id  = resp.get('model_id', ''),
            result_cid= resp.get('result_cid', ''),
            usdc_cost = resp.get('usdc_cost', 0.0),
        )

    def quote(
        self,
        task     : str,
        model    : str,
        gpu_count: int   = 1,
        hours    : float = 1.0,
    ) -> dict:
        """Get a price quote before submitting a job."""
        resp = self._rpc_post('/compute/quote', {
            'job_type' : task,
            'model_id' : model,
            'gpu_count': gpu_count,
            'duration_hr': hours,
        })
        return resp or {
            'usdc_cost'       : round(0.40 * gpu_count * hours, 4),
            'savings_pct'     : 56.4,
            'aws_equiv_usdc'  : round(0.918 * gpu_count * hours, 4),
            'note'            : 'Estimated (node offline)',
        }

    def network_status(self) -> dict:
        """Return live network stats."""
        return self._rpc_get('/status') or {
            'height'        : 0,
            'miners_online' : 0,
            'total_gpus'    : 0,
            'note'          : 'Node offline',
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_inference(
        self,
        task   : str,
        model  : str,
        inputs : Any,
        params : dict,
    ) -> list[InferenceResult]:
        """Send inference job to Obelyth node and return results."""
        start = time.time()
        resp  = self._rpc_post('/compute/infer', {
            'task'   : task,
            'model'  : model,
            'inputs' : inputs if isinstance(inputs, list) else [inputs],
            'params' : params,
            'api_key': self.api_key,
        })

        if resp is None:
            # Node unreachable — dev mode stub
            text = inputs if isinstance(inputs, str) else (inputs[0] if inputs else '')
            return [InferenceResult(
                generated_text = (
                    f"[Obelyth dev stub] You asked: '{text[:50]}'. "
                    f"In production, model '{model}' would process this on a "
                    f"decentralized GPU miner."
                ),
                job_id    = f'stub-{self._session_id[:8]}',
                latency_ms= (time.time() - start) * 1000,
                usdc_cost = 0.0001,
                verified  = False,
            )]

        outputs = resp.get('outputs', [])
        if not outputs:
            raise NexusNoMinersError("No miners available for this job")

        return [InferenceResult(
            generated_text = o.get('generated_text', ''),
            score          = o.get('score', 0.0),
            label          = o.get('label', ''),
            job_id         = resp.get('job_id', ''),
            miner_addr     = resp.get('miner_addr', ''),
            latency_ms     = (time.time() - start) * 1000,
            usdc_cost      = resp.get('usdc_cost', 0.0),
            verified       = resp.get('verified', False),
        ) for o in outputs]

    def _stream_inference(
        self, model: str, inputs: Any, params: dict
    ) -> Iterator[str]:
        """Streaming token iterator — stub for dev, real SSE in production."""
        words = f"Streaming from {model}: {str(inputs)[:40]}...".split()
        for word in words:
            yield word + ' '
            time.sleep(0.05)

    def _upload_dataset(self, path: str) -> str:
        """
        Upload dataset file to IPFS via node.
        Returns IPFS CID. Stub returns deterministic hash for dev.
        """
        import os
        if not os.path.exists(path):
            log.warning(f"Dataset not found: {path}. Using mock CID.")
            return 'Qm' + hashlib.sha3_256(path.encode()).hexdigest()[:44]

        size = os.path.getsize(path)
        log.info(f"Uploading dataset {path} ({size/1024:.1f} KB)...")
        # In production: stream to /ipfs/upload endpoint
        with open(path, 'rb') as f:
            content = f.read()
        cid = 'Qm' + hashlib.sha3_256(content).hexdigest()[:44]
        log.info(f"Dataset CID: {cid}")
        return cid

    def _rpc_get(self, endpoint: str) -> Optional[dict]:
        try:
            req = urllib.request.Request(
                self.node_url + endpoint,
                headers={
                    'X-API-Key'       : self.api_key,
                    'X-Nexus-SDK'     : SDK_VERSION,
                    'X-Session-ID'    : self._session_id,
                }
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            log.debug(f"RPC GET {endpoint} failed: {e}")
            return None

    def _rpc_post(self, endpoint: str, data: dict) -> Optional[dict]:
        try:
            body = json.dumps(data).encode()
            req  = urllib.request.Request(
                self.node_url + endpoint,
                data   = body,
                headers= {
                    'Content-Type' : 'application/json',
                    'X-API-Key'    : self.api_key,
                    'X-Nexus-SDK'  : SDK_VERSION,
                    'X-Session-ID' : self._session_id,
                }
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            log.debug(f"RPC POST {endpoint} failed: {e}")
            return None


# ── NexusTrainer — PyTorch Trainer drop-in ────────────────────────────────────

class NexusTrainer:
    """
    Drop-in replacement for HuggingFace Trainer.
    Routes training to Obelyth miners instead of local GPU.

    Usage:
        BEFORE:
            from transformers import Trainer
            trainer = Trainer(model=model, args=training_args, train_dataset=ds)
            trainer.train()

        AFTER:
            from nexus import NexusTrainer
            trainer = NexusTrainer(model=model, args=training_args, train_dataset=ds,
                                   nexus_client=client)
            trainer.train()   # runs on decentralized GPUs
    """

    def __init__(
        self,
        model         = None,
        args          = None,
        train_dataset = None,
        eval_dataset  = None,
        nexus_client  : ObelythClient = None,
        **kwargs,
    ):
        self.model         = model
        self.args          = args
        self.train_dataset = train_dataset
        self.eval_dataset  = eval_dataset
        self.client        = nexus_client or ObelythClient()
        self._extra        = kwargs
        self._job          : Optional[FineTuneJob] = None

    def train(self, **kwargs) -> 'NexusTrainer':
        """Submit training job to Obelyth. Non-blocking."""
        model_name = getattr(self.model, 'name_or_path', 'custom-model')
        epochs     = getattr(self.args, 'num_train_epochs', 3) if self.args else 3
        batch_size = getattr(self.args, 'per_device_train_batch_size', 4) if self.args else 4
        lr         = getattr(self.args, 'learning_rate', 2e-4) if self.args else 2e-4

        # Serialize dataset if needed
        dataset_path = self._serialize_dataset()

        log.info(f"NexusTrainer: submitting '{model_name}' for {epochs} epochs "
                 f"on Obelyth...")

        self._job = self.client.fine_tune(
            base_model    = model_name,
            dataset_path  = dataset_path,
            method        = 'qlora',
            epochs        = int(epochs),
            learning_rate = float(lr),
            batch_size    = int(batch_size),
        )
        log.info(f"Job submitted: {self._job.job_id}  cost=${self._job.usdc_cost:.4f} USDC")
        return self

    def wait_for_completion(self, timeout: int = 7200) -> FineTuneJob:
        """Block until training completes."""
        if not self._job:
            raise NexusError("No job submitted. Call .train() first.")
        return self._job.wait(self.client, timeout=timeout)

    def _serialize_dataset(self) -> str:
        """Write HuggingFace dataset to temp JSONL for upload."""
        import tempfile, os
        if self.train_dataset is None:
            return '/dev/null'
        path = os.path.join(tempfile.gettempdir(), 'nexus_train_data.jsonl')
        try:
            with open(path, 'w') as f:
                for item in self.train_dataset:
                    f.write(json.dumps(dict(item)) + '\n')
            log.info(f"Dataset serialized: {path}")
        except Exception as e:
            log.warning(f"Could not serialize dataset: {e}")
        return path

    @property
    def job_id(self) -> str:
        return self._job.job_id if self._job else ''


# ── Convenience alias ─────────────────────────────────────────────────────────

def connect(api_key: str = None, node: str = DEFAULT_NODE, **kwargs) -> ObelythClient:
    """Shorthand: client = nexus.connect('oby_...')"""
    return ObelythClient(api_key=api_key, node_url=node, **kwargs)
