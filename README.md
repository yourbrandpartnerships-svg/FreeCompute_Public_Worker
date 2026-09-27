# FreeCompute Public Worker

This repository is intentionally public and contains only bounded GitHub Actions workflows used by the independent FreeCompute service.

It contains no SHOREA source code, provider credentials, private datasets, arbitrary shell execution, or paid-runner configuration.

## V8 cloud accelerator

The repository also runs the SHOREA V8 public support accelerator on GitHub-hosted `ubuntu-latest`.

- GitHub-hosted CPU support runs every 30 minutes.
- Kaggle GPU support is optional and fail-closed until an encrypted Actions secret is configured.
- Kaggle uses `ROUND_ROBIN_NEW_TASKS_ONLY` semantics across the configured identity lanes.
- The worker reads live Kaggle GPU quota before dispatch and preserves a reserve.
- No SHOREA private source, customer data, provider secret, or paid compute path is placed in this public repository.

### One-time Kaggle GPU activation

In this repository, open **Settings → Secrets and variables → Actions → New repository secret**.

Add at least one of:

- `FC_KAGGLE_OWNER_API_TOKEN`
- `FC_KAGGLE_COLLAB1_API_TOKEN`
- `FC_KAGGLE_COLLAB2_API_TOKEN`

Optional username secrets are also accepted, but the cloud worker attempts to derive the Kaggle username from the authenticated API token.

Do not commit API-token values to Git or place them in workflow inputs, logs, issues, or artifacts.

Once a token is present, no Windows runner or PowerShell startup is required. The scheduled cloud workflow detects the token automatically, checks live GPU quota, and runs bounded Kaggle GPU support work when the reserve gate allows it.
