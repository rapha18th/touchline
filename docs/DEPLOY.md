# Deploy

Touchline runs as one container on Azure Container Apps. The image is built in Azure Container Registry. The app signs in to the Foundry models with its managed identity, so no key exists anywhere. There are three ways in and one way out.

| What | Where | When |
| --- | --- | --- |
| CI | `.github/workflows/ci.yml` | Every push to `main` or `ws/**`, every pull request, or on demand |
| CD | `.github/workflows/cd.yml` | On demand, from `main` |
| Local deploy | `infra/deploy.sh` | From a machine that holds `data/matches` |
| Teardown | `infra/teardown.sh` | When the entry is over |

## CI

Three jobs. None of them signs in to Azure.

1. **Engine and live service tests.** Python 3.12 on Ubuntu. Installs `engine`, `services/live` and `vision` with their `dev` extras. `TOUCHLINE_DATA` points at an empty folder, so no test reads a local match. The engine tests run first. The schema contracts run as their own step: a short match is generated fresh, and every event is validated against `schema/event.schema.json` and every cue against `schema/cue.schema.json` with `jsonschema`. The recorded broadcasts in `broadcasts/` are validated too. Then the live service tests, the vision tests and the staging tests in `infra/tests` run.
2. **Web modules parse.** `scripts/check_js.sh` runs `node --check` on every file in `web/js`. Each file is checked as an ES module, the way the browser loads it.
3. **Image.** Stages the build context with `infra/stage.py --allow-missing`, builds the Dockerfile from it with an empty `SEEDS`, runs the container and waits for `/healthz`. The matches are not in git, so this build ships none, and with `SEEDS` empty it generates none. It proves the Dockerfile and the stage agree and that the service starts.

Run the same checks locally:

```bash
python -m pytest engine -q
python -m pytest services/live -q
python -m pytest vision/tests -q
python -m pytest infra/tests -q
bash scripts/check_js.sh web
```

## The build context

`infra/stage.py` assembles the build context in the system temp folder, `%TEMP%\touchline-build` on Windows. The Dockerfile is the manifest. Every path it copies is staged and nothing else is. `data/matches` is narrowed to the three showcase matches named by the Dockerfile's `SEEDS`: TL-000008, TL-000009 and TL-000012. Their recorded broadcasts and voiced clips ship in `broadcasts/`. Any other match in the local data folder is left out on purpose, and so is `web/capture.html`, the vision capture harness.

```bash
python infra/stage.py                  # prints the staged path; the summary goes to stderr
python infra/stage.py --out DIR        # stage somewhere else
python infra/stage.py --matches DIR    # read the matches from another folder
python infra/stage.py --allow-missing  # stage without matches; the build generates them
```

Measured on 26 October 2026 at commit 2e67f47, from the stage's `.touchline-stage.json`:

| Part of the staged context | On disk |
| --- | ---: |
| `data/matches`: the three showcase matches | 33.9 MB |
| `broadcasts`: three recorded broadcasts and 557 voiced clips | 24.6 MB |
| `services`, `web`, `engine`, `schema` and the Dockerfile | 0.5 MB |
| All 622 files | 59.0 MB |
| Gzipped upload | 52.9 MB |

The three matches and the clips are almost all of it. The matches' `tracking.npz` files and the mp3 clips are already compressed, so gzip saves little on them. `.touchline-stage.json` in the stage records the commit, the file count, the sizes, and a hash of each staged match's events.

## Local deploy

```bash
export PATH="/c/Program Files/Microsoft SDKs/Azure/CLI2/wbin:$PATH"
az login
bash infra/deploy.sh
```

The script stages the context, builds it in ACR, and rolls the Container App to a new revision. On a first run it creates the registry, the Container Apps environment and the app with a system identity, and grants that identity AcrPull on the registry and `Cognitive Services OpenAI User` on the Foundry account, which must already exist. Each rollout sets `TOUCHLINE_CREW=1` and the Foundry endpoint on the app and holds it at one replica. It is idempotent. It survives a flaky link:

- Every Azure call that can drop is retried, up to `RETRIES` times (default 5) with a growing pause.
- The upload is the step that fails most. `az acr build` is retried as a whole.
- The build is queued with `--no-wait` and polled every 15 seconds. No long connection is held.
- The rollout is queued with `--no-wait`. The script waits for the new revision by name, so the old revision's state cannot pass for the new one.
- `MSYS_NO_PATHCONV=1` stops Git Bash from rewriting `/subscriptions/...` scopes into file paths.

Useful overrides:

```bash
SKIP_BUILD=1 TAG=567851e bash infra/deploy.sh         # roll out an image already in ACR
STAGE_ARGS="--matches D:/matches" bash infra/deploy.sh
RETRIES=8 POLL_S=10 bash infra/deploy.sh
```

## CD with OpenID Connect

The CD workflow signs in with a federated credential. GitHub issues a short-lived token for the run. Entra trusts tokens for this repository's `main` branch and nothing else. No client secret is created or stored.

The workflow stages the context, runs `az acr build`, runs `az containerapp update` to a revision named after the commit, polls until that revision is live, and checks `/healthz`.

### One-time setup

Run these once, signed in as an owner of the subscription, in Git Bash. They create one Entra app registration, its service principal, one federated credential and one role assignment.

```bash
export MSYS_NO_PATHCONV=1
export PATH="/c/Program Files/Microsoft SDKs/Azure/CLI2/wbin:$PATH"
REPO=rapha18th/touchline
RG=rg-touchline
SUB=$(az account show --query id -o tsv)
TENANT=$(az account show --query tenantId -o tsv)

# 1. The deploy identity.
APP_ID=$(az ad app create --display-name touchline-github-deploy --query appId -o tsv)
SP_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)

# 2. Trust GitHub's tokens for the main branch of this repository.
cat > fic.json <<EOF
{
  "name": "github-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:$REPO:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"],
  "description": "Touchline CD workflow on main"
}
EOF
az ad app federated-credential create --id "$APP_ID" --parameters fic.json
rm fic.json

# 3. Let it build in the registry and update the app. Both live in the resource group.
az role assignment create --assignee-object-id "$SP_ID" --assignee-principal-type ServicePrincipal \
  --role Contributor --scope "/subscriptions/$SUB/resourceGroups/$RG"

# 4. Tell the workflow who to sign in as. These three values only identify; they grant nothing.
gh variable set AZURE_CLIENT_ID --body "$APP_ID" --repo "$REPO"
gh variable set AZURE_TENANT_ID --body "$TENANT" --repo "$REPO"
gh variable set AZURE_SUBSCRIPTION_ID --body "$SUB" --repo "$REPO"
```

Contributor on the resource group is the simplest grant that covers `az acr build` and `az containerapp update`. It reaches nothing outside `rg-touchline`.

CD expects the Container App to exist. `infra/deploy.sh` creates it, has it pull from ACR with its own system identity, and grants that identity `Cognitive Services OpenAI User` on the Foundry account. CD changes neither.

### Run it

```bash
gh workflow run cd.yml --repo rapha18th/touchline --ref main
gh run watch --repo rapha18th/touchline
```

Set `roll_out` to false to build the image and leave the Container App on its current revision:

```bash
gh workflow run cd.yml --repo rapha18th/touchline --ref main -f roll_out=false
```

## Teardown

```bash
bash infra/teardown.sh          # lists every Touchline resource; deletes nothing
bash infra/teardown.sh --yes    # lists it, then deletes it
```

Without `--yes` the script only reads. With it, the script deletes the resource group `rg-touchline` and everything in it: the Container App, its environment, the logs workspace, Application Insights, the registry and the Foundry account with its model deployments. Role assignments scoped to those resources go with them. Then it purges the soft-deleted Foundry account, which Azure otherwise holds for 48 hours under its name. Last, it deletes the `touchline-github-deploy` app registration if the CD setup above was followed. Deletion is queued with `--no-wait` and followed by short polls. A rerun resumes where a dropped run stopped.

Other resource groups whose name holds `touchline` are listed and left alone. Add them with `RGS="rg-touchline other-rg"`.

## Limits

- CI runs on GitHub. No Docker engine ran on the development machine, so the CI image job is where the slim context is built and started.
- The CD workflow has not run on GitHub yet. Its file parses, and every `run:` block in both workflows passes `bash -n`.
- CD has no `data/matches`. Its image build generates the three showcase seeds from the engine, which is deterministic per seed on one platform. That a Linux build reproduces the Windows-generated files byte for byte, so the recorded broadcasts line up frame for frame, is not verified. `infra/deploy.sh` run from a machine that holds the generated matches ships the exact files.
- `infra/deploy.sh` has not completed a run from the staged context. `infra/teardown.sh --yes` has not run against Azure. `infra/teardown.sh` without `--yes` has, and listed the resources in `rg-touchline`.
