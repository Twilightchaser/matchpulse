# Deploy MatchPulse without ACR Tasks

## Current release — October 24, 2026: 0.6.2

- Source commit: `d77a60b4974a4df0f83e5bd2f6f4f617ed4b1040`
- Successful build: https://github.com/Twilightchaser/matchpulse/actions/runs/40083191051
- Deployed image: `ghcr.io/twilightchaser/matchpulse@sha256:b66158c64335a1ba58d0168361a1c7e58f28d6c91ee5d15d6174b7aa39b1081b`
- `/api/health` reports `0.6.2` and provider `deepseek`.
- The 22 demo players use distinct fictional names. The optional browser voice control reads verified cards in English or Chinese when enabled, with automatic speech skipped above 2× replay speed. Browser and installed voice support vary.
- On the live site, the seeded goals remain at 17:46, 43:41 and 57:17. At the first goal, the Chinese card names Luca Wren and shows 1–0, 0.27 xG and 87.4 km/h. The browser displayed no console errors during this check.

The two-minute video, final submission and an authentic Guardian numeric-rejection recording remain unfinished. Browser audio has been exercised through its control and a JavaScript speech-engine test; pronunciation and output quality must be checked by listening on the recording machine.

## Previous release — October 24, 2026: 0.6.1

- Live site: https://matchpulse.blueriver-19b29690.eastasia.azurecontainerapps.io/
- Source commit: `5813f3d0b94fdcfe96c5ff4a3410300ba353be70`
- Successful build: https://github.com/Twilightchaser/matchpulse/actions/runs/39976562933
- Deployed image: `ghcr.io/twilightchaser/matchpulse@sha256:2cac2eec784442093c378bd751d3e9e6f3578bef15212ca7741ca5caf275f418`
- `/api/health` reports version `0.6.1` and provider `deepseek`. The HTML loads `/static/app.js?v=0.6.1` with `Cache-Control: no-cache`.
- At 17:46, the first goal marker lands on the goal, shows Northbridge FC 1–0 Riverside Athletic, and displays a verified Chinese card with 0.27 xG and 87.4 km/h shot speed. The event feed is rebuilt in Chinese after switching language.
- CI passed 94 Python tests, JavaScript syntax and frontend checks, plus container smoke testing. A live replay sample showed 41 events and 42 state updates while an AI narrative call was in flight.
- The model API key remains an Azure Container Apps secret and is not part of the repository or distribution ZIP.

Recording and submission are still pending. The real numeric Evidence Guardian rejection requested for the video has not yet been captured; an actual Chinese language rejection with a safe fallback has been recorded. Live one-second latency is not guaranteed, especially after a cold start. The simulator is an illustrative synthetic match, not a production tracking system.

## Initial deployment — October 23, 2026

This package adds GitHub Actions image publishing to the supplied v0.6 source. The application, simulator and Dockerfile are unchanged. The deployment below was completed and checked on October 23, 2026.

## Live deployment and verification

- Website: https://matchpulse.blueriver-19b29690.eastasia.azurecontainerapps.io/
- Source: https://github.com/Twilightchaser/matchpulse
- Successful build: https://github.com/Twilightchaser/matchpulse/actions/runs/39934854178
- Published application commit: `6122e3def13629418342366f3e3f5f8a3282020f`
- Deployed image: `ghcr.io/twilightchaser/matchpulse@sha256:293248ca3d6d58184f120acec13163d644b5359cdbab5297fe868fe6109a37c8`
- Azure revision: `matchpulse--latest`; revision health was `Healthy`, running state `Running`.
- Resources: 1 vCPU, 2 GiB, minimum 0 replicas and maximum 1 replica. Idle scale-to-zero means a possible cold start. This is not a zero-cost guarantee.
- Model: `deepseek-flash`; API key stored as Azure secret `deepseek-api-key` and referenced by `LLM_API_KEY`. No model or Azure credentials were uploaded to GitHub.

The Linux/Python 3.12 image passed all 91 Python tests, JavaScript syntax, localization and replay-control checks, and a container startup health check. CI installs the optional Foundry testing dependencies in a disposable test container; they are not added to the production image.

Live checks confirmed an actual DeepSeek-generated English goal insight, a Chinese analyst insight and Chinese full-time recap, with evidence validation. The replay, pause, timeline seeking and language switch were exercised in the browser; the browser error log was empty. The dataset endpoint returned 2,860 synthetic events, and the deterministic overlay export returned 29 cards with `model_calls: 0`.

Deployment is complete, but these checks are not full submission acceptance. The first uncached English insight took about 3.4 seconds end to end, above the proposed one-second target. Generated Chinese copy still included English terminology such as `momentum share`, and a replay status line retained an English event label after switching languages. These need product polish before recording. A full 1920x1080 recording, the authentic Guardian rejection scene and the final submission are still separate tasks.

For reproducible simulation snapshots, use the Dockerfile's Python 3.12 runtime. A separate local Python 3.14 run differed in two deterministic simulation assertions; those assertions passed in the deployed Linux/Python 3.12 image.

## Why this route

The Azure subscription rejected ACR Tasks with `TasksOperationsNotAllowed`. Source deployment through `az containerapp up --source .` invokes ACR Tasks when a Dockerfile is present. Microsoft documents a pause on ACR task runs funded by free credits:
https://learn.microsoft.com/en-us/azure/container-registry/container-registry-tasks-overview

This route builds on a GitHub-hosted runner and publishes to GitHub Container Registry (GHCR). Azure Container Apps then runs the existing image. Azure credentials and model credentials are not required in GitHub.

## 1. Upload the source to GitHub

Create or use a public GitHub repository, preferably named `matchpulse`, with default branch `main`. Extract the ZIP and upload the CONTENTS into the repository root. The repository root must contain `Dockerfile`, `pyproject.toml`, `app/`, and `.github/workflows/publish-image.yml`.

Do not upload only the ZIP. Include the `.github` folder and the `.dockerignore` file. Never upload `.env`, `matchpulse.env` or real credentials. The example configuration file is safe to include.

## 2. Build the image

Open Actions -> Build MatchPulse image. A push to `main` starts a run, or choose Run workflow after the workflow exists on the default branch. The run builds the Linux image, starts it without model credentials, checks `/api/health`, and only then publishes it to GHCR. The workflow uses the automatically supplied GITHUB_TOKEN with packages:write permission.

Wait for the run to succeed. If it fails, inspect the failing step instead of continuing with deployment. The health check proves container startup, not DeepSeek connectivity or the full feature acceptance criteria.

## 3. Make the IMAGE public

From the repository's Packages section (or your GitHub profile's Packages tab), open the container package. Open Package settings -> Change visibility -> Public, and complete GitHub's confirmation.

Check package visibility separately from repository visibility. This project's package was verified as Public. Azure can pull a public GHCR image without GitHub credentials.

https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry

## 4. Deploy the existing image

Open the successful workflow run's Summary. Copy its generated Azure command into Azure Cloud Shell (Bash). The command uses the exact published image digest and these existing resource names:

- Resource group: `matchpulse-rg`
- Container Apps environment: `matchpulse-env-eastasia`
- Container app: `matchpulse`
- Region: `eastasia` (present in this subscription's allowed-location policy)
- Public ingress target port: `8000`

The command uses `--image`, not `--source`, and does not need a source directory in Cloud Shell. If the environment was left in a failed provisioning state, inspect that resource before retrying; do not repeatedly create new environments.

https://learn.microsoft.com/en-us/azure/container-apps/containerapp-up#deploy-from-an-existing-image

## 5. Configure and verify DeepSeek

In the Azure container app, add a Container Apps secret named `deepseek-api-key` with your key. Create a new revision and configure these container environment variables:

| Name | Value |
| --- | --- |
| LLM_API_KEY | Secret reference to `deepseek-api-key` |
| LLM_BASE_URL | `https://api.deepseek.com` |
| LLM_MODEL | The model name that works in your local configuration |
| LLM_TIMEOUT_SECONDS | `20` |

Start with one maximum replica for this in-memory demo. Choose zero minimum replicas for idle scale-to-zero, or one minimum replica for a warm recording/judging session; a warm replica consumes resources. Apply sizing and scaling settings in a new revision before final testing.

Open the public URL, check playback and English/Chinese switching, then verify an actual model-generated insight and recap. `/api/health` alone does not prove that the model endpoint is responding. Do not share keys in screenshots or terminal output.

## Existing ACR resource

The failed source deployment created registry `cab031d86abbacr`. This GHCR route does not use it. After confirming it contains nothing needed, remove that unused registry through the Azure portal to avoid unnecessary registry charges. Keep the Container Apps environment and resource group used above.
