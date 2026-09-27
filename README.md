# FreeCompute Public Worker

This repository is intentionally public and contains only bounded GitHub Actions workflows used as a sanitized public/synthetic support lane for the independent FreeCompute + SHOREA architecture.

It contains no SHOREA private source code, provider credentials, private datasets, arbitrary shell execution, or paid-runner configuration.

## V8 public evidence farm

The V8 cloud worker runs checkpoint-aware synthetic CPU evidence on GitHub-hosted ubuntu-latest.

Current workload families are V8-CP3 agent_provider_learning, V8-CP4 revenue_product_ranking, V8-CP5 market_reputation_trend, V8-CP6 ops_truth_reconciliation, V8-CP7 failure_anomaly_hardening, V8-CP8 canary_incrementality, V8-CP9 reconciliation_drift, and V8-CP10 evidence_monitoring_decay.

The worker produces support-only evidence with scenario counts, pass rate, Wilson 95% lower bound, runtime metadata, SHA-independent applicability, expiration, and an artifact hash.

## Provider-compute boundary

FreeCompute is the sole provider-compute authority.

This public repository does not authenticate to Kaggle and does not hold Kaggle owner/collaborator tokens. It does not select provider identities, read provider quota, launch provider notebooks, retry provider jobs, or retrieve provider artifacts.

Kaggle CPU/GPU routing, identity isolation, quota reserve, CUDA verification, retries/backpressure, and provider artifacts belong to the private FreeCompute control plane in yourbrandpartnerships-svg/Free_VM_GPU.

Any sanitized provider-compute evidence that SHOREA consumes must arrive through FreeCompute's bounded evidence path. Public-worker evidence can strengthen checkpoint confidence but is never an authoritative merge/transition gate.

## Safety properties

- public/synthetic inputs only
- no provider credentials
- no private SHOREA source
- no paid compute path
- no automatic card-backed fallback
- support_only is true
- authoritative_merge_gate is false
- GPU is never claimed by this worker
