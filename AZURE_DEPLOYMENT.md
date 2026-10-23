# Deploy MatchPulse without ACR Tasks

This package adds GitHub Actions image publishing to the supplied v0.6 source. It does not change the application, simulator, dependencies or Dockerfile. The workflow has been prepared locally; a successful GitHub run is still required to verify the actual image build.

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

The image package is private on first publication even when the repository is public. Azure can pull a public GHCR image without GitHub credentials. Both the code repository and image package need public visibility for this path.

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
