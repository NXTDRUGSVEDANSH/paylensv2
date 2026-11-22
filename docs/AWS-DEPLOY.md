# Deploying PayLens on EC2

Live at <http://ec2-54-159-33-138.compute-1.amazonaws.com/>

Push to `main` → GitHub Actions SSHes into the box → `git pull` → `docker build`
→ restart the container. No registry, no OIDC, no AWS APIs: the box builds its
own image from the code it just pulled, which is the least machinery that can
work for one small service.

The workflow is `.github/workflows/deploy-aws.yml`.

---

## What is on the box

| | |
|---|---|
| Instance | `t3.small` (2 GB) — see the sizing note below |
| User | `ubuntu` |
| Project | `/home/ubuntu/projects/paylensv2` |
| Image / container | `paylens` |
| Ports | host `80` → container `8000` |
| Secrets | `/home/ubuntu/projects/paylensv2/.env`, never touched by the deploy |

**2 GB is the floor, not a preference.** The warm process measures ~465 MB, and
DuckDB takes its configured budget on top. A 1 GB instance boots and then dies
on the first real question.

The image is built `--build-arg WITH_KB=0`, which leaves chromadb and
onnxruntime out — roughly 600 MB less disk and 250 MB less RAM, and the reason
this fits a `t3.small` at all. **That build requires `DISABLE_CHROMA=true` in
`.env`**; without it the app reaches for a store that is not installed. The
deploy checks for that line and fails early rather than shipping a container
that starts and then misbehaves.

What you give up: few-shot retrieval and the definitional corpus. Measurement,
decomposition and advice are unaffected — verified by running the app with
Chroma disabled and asking a question end to end.

---

## One-time setup on EC2

A key dedicated to deployment, so it can be revoked without touching your own
access.

```bash
# on the EC2 box, as ubuntu
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# this is what goes into the GitHub secret - the PRIVATE key, whole file
cat ~/.ssh/github_deploy
```

Confirm the rest is in place:

```bash
cd /home/ubuntu/projects/paylensv2
git remote -v                     # should point at the GitHub repo
git fetch origin main             # SSH auth to GitHub must already work
test -f .env && echo ".env present"
grep -q '^DISABLE_CHROMA=true' .env && echo "DISABLE_CHROMA set"
docker ps                         # the ubuntu user must be in the docker group
```

If `docker ps` needs `sudo`, add the user and reconnect — the deploy does not
use `sudo`:

```bash
sudo usermod -aG docker ubuntu && exit
```

Security group: allow inbound **80** from anywhere and **22** from your own
address. Port 8000 need not be open; only the container binds it.

---

## GitHub secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `EC2_HOST` | `ec2-54-159-33-138.compute-1.amazonaws.com` (or the public IPv4) |
| `EC2_USER` | `ubuntu` |
| `EC2_SSH_KEY` | the **private** key printed above — the entire file, including the `BEGIN`/`END` lines and the trailing newline |

A truncated key is the single most common failure here, and it surfaces as a
confusing handshake error rather than anything that mentions truncation.

---

## Testing it

**1 · Dry run on the box**, before trusting the pipeline. This is exactly what
the workflow does:

```bash
cd /home/ubuntu/projects/paylensv2
git pull
docker build --build-arg WITH_KB=0 -t paylens .
docker rm -f paylens 2>/dev/null || true
docker run -d --name paylens --restart unless-stopped \
  --env-file .env -p 80:8000 paylens
curl -s http://localhost/health
```

Expect `"views_built":true` and `"llm_available":true`. The first build takes
several minutes — the dataset is generated and verified inside it.

**2 · Trigger the workflow manually.** Actions → *deploy to ec2* → Run
workflow. Watch it pull, build, restart and report healthy.

**3 · Push something trivial to `main`** and confirm it deploys on its own.

**4 · Ask the service a real question**, which the health check does not cover:

```bash
curl -s -X POST http://ec2-54-159-33-138.compute-1.amazonaws.com/v2/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"what was the success rate last week","today":"2026-11-20"}'
```

---

## When it fails

The workflow prints the last health response, the container logs and
`docker ps -a` before exiting non-zero, so the Actions log usually contains the
answer already.

| Symptom | Cause |
|---|---|
| `.env is missing on the host` | the file was moved or the path changed |
| `must set DISABLE_CHROMA=true` | the image has no chromadb; add the line |
| `views_built` false | the build did not create the views — read the build log |
| `llm_available` false | `LLM_API_KEY` missing or wrong in `.env` |
| SSH handshake fails | the secret is truncated, or the public key is not in `authorized_keys` |
| build killed partway | memory. `t3.small` is the floor; builds are the peak |
| port 80 refused | security group, or the container exited — check `docker ps -a` |

Health is deliberately `views_built` rather than "the process responded". A
container that boots cleanly and then refuses every question is a failure this
deployment has actually had, and a liveness check calls it healthy.
