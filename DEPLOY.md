# Deploying PayLens on AWS

## The thing that makes this easy

I checked the code for state before writing any of this. PayLens is **immutable and
stateless**, which removes most of the usual deployment work:

| Surface | Finding |
|---|---|
| DuckDB | opened **read-only**, 256 MB memory cap (`backend/core/db.py`) |
| Chroma | deterministic from `semantic/seeds.yaml` — a **build artifact**, not runtime state |
| Answer caches | in-process LRU, TTL'd — losing them on restart costs latency, never correctness |
| Runtime writes | only optional KB "learned" records, which are disposable |
| Uploads / sessions / user data | none |

So **no RDS, no EFS, no ElastiCache, no volumes.** Bake the data into the image and
scale by adding tasks. That is the whole architecture.

One more finding worth the ~267 MB it saves: `pandas`, `pyarrow` and `numpy` appear
in **zero** serving-path imports — they are used only by `data/generate.py`, which
runs at build time. `requirements-runtime.txt` excludes them.

```
slim  (DISABLE_CHROMA=true)  ~89 MB of wheels   + 23 MB data
full  (semantic cache)      ~239 MB of wheels   + 23 MB data + 167 MB ONNX model
```

---

## Local deployment (do this first)

One command. Builds the venv, installs deps, generates **and verifies** the dataset,
then installs a systemd user service that restarts on failure and comes back at login.

```bash
./deploy/local/install.sh
```

```
▸ venv exists: /home/you/.venvs/paylens
▸ installing dependencies
▸ dataset present (REGENERATE=1 to rebuild)
▸ verifying calibration and planted defects
  ALL CHECKS PASSED — data matches the NPCI calibration and the planted incident.
▸ installing systemd user service
▸ lingering enabled — service survives logout
▸ waiting for health (the ONNX model load takes ~25s on a cold start)
{ "status": "ok", "transactions": 777757, "kb_records": 29 }
▸ PayLens is up
    http://localhost:8000
```

| | |
|---|---|
| status | `systemctl --user status paylens` |
| logs | `journalctl --user -u paylens -f` |
| restart | `systemctl --user restart paylens` |
| stop | `systemctl --user stop paylens` |
| rebuild data | `REGENERATE=1 ./deploy/local/install.sh` |
| remove | `./deploy/local/uninstall.sh` (keeps venv + data) |

It is idempotent — re-run it after any code change and it reinstalls the unit and
restarts.

**Why a service and not just `uvicorn ...`:** the install was verified by `SIGKILL`ing
the process; systemd brought it back in ~12 s and `/health` returned `ok`. A terminal
job dies with the terminal, and mid-demo that is the worst possible moment.

**Verification is a gate, not a step.** The script runs `data/verify.py` and *refuses
to deploy* if the NPCI calibration has drifted, so a stale or broken dataset cannot
reach a running service.

### Reaching it from Windows

WSL2 forwards localhost, so **http://localhost:8000** works in a Windows browser
unchanged. If a VPN or firewall breaks that, the script prints the direct WSL address
(`http://172.x.x.x:8000`) as a fallback — note it changes on WSL restart.

### If Docker *is* what you want

Install Docker Desktop for Windows and enable WSL2 integration in its settings; then
`docker` appears in this shell and `docker build -t paylens .` works. It is not
required for local use — the systemd route above is lighter and starts faster.

---

## Pick a target

| Option | Use when | Cost (ap-south-1, rough) | Effort |
|---|---|---|---|
| **App Runner** | You want a URL and HTTPS and nothing else to think about | ~$25–45/mo (1 vCPU / 2 GB, always on) | **Lowest — recommended** |
| **ECS Fargate + ALB** | You need a VPC, private subnets, autoscaling policies, WAF | ~$35/mo task + ~$18/mo ALB | Medium |
| **EC2 `t3.small` + Caddy** | Assignment demo, cheapest, you're fine with one box | ~$15/mo | Low, but you own patching |
| **Lambda (container)** | Spiky, low traffic, and you accept cold starts | near-zero idle | **Not recommended — see below** |

### Why not Lambda

It will technically work as a container image (the 250 MB zip limit doesn't apply,
and 10 GB does), but three things fight you:

1. **Cold start.** Importing `chromadb` + `onnxruntime` and memory-mapping a 23 MB
   DuckDB file is several seconds before the first byte. The slim build is far
   better, but you've then given up the semantic cache.
2. **The in-process caches evaporate** between invocations, so every request pays
   full price — exactly the latency the caching layer exists to remove.
3. **The watchdog timeout** in `executor.py` is a `threading.Timer` calling
   `interrupt()`. It works, but Lambda's own freeze/thaw makes wall-clock timeouts
   less predictable.

If you want serverless economics anyway, use the **slim build** with
`DISABLE_CHROMA=true` and provisioned concurrency of 1.

---

## Recommended: App Runner in ~10 minutes

```bash
export AWS_REGION=ap-south-1

# 1. Build + push (creates the ECR repo if missing)
./deploy/push-ecr.sh

# 2. Store the LLM key. Never put it in an env var in plaintext —
#    the app runs fine without one, so there is no excuse to rush this.
aws secretsmanager create-secret \
  --name paylens/llm-api-key \
  --secret-string 'sk-or-v1-...' \
  --region $AWS_REGION

# 3. Create the service (console is honestly fine here; CLI shown for the record)
#    Edit deploy/apprunner.yaml first: <ACCOUNT_ID> and CORS_ORIGINS.
```

In the App Runner console: **Create service → Container registry → ECR →
`paylens:latest`**, port `8000`, health check path `/health`, 1 vCPU / 2 GB.
Give the instance role `secretsmanager:GetSecretValue` on that one secret ARN.

You get an HTTPS URL. Done. Auto-deploy on new `:latest` pushes is a checkbox.

### ECS Fargate instead

`deploy/ecs-task-definition.json` is ready — replace `<ACCOUNT_ID>`, then:

```bash
aws ecs register-task-definition --cli-input-json file://deploy/ecs-task-definition.json
aws ecs create-service --cluster paylens --service-name paylens \
  --task-definition paylens --desired-count 2 --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-a,subnet-b],securityGroups=[sg-x],assignPublicIp=DISABLED}' \
  --load-balancers 'targetGroupArn=<TG_ARN>,containerName=paylens,containerPort=8000'
```

ALB target group: health check `/health`, **healthy threshold 2, interval 30s,
and a 90-second deregistration/slow-start**, because the full build spends ~25 s
loading the ONNX model at startup. Set that or the first task gets killed before it
is ready and you will chase a phantom crash loop.

### Cheapest: one EC2 box

```bash
# Amazon Linux 2023, t3.small, 20 GB gp3
sudo dnf install -y docker && sudo systemctl enable --now docker
aws ecr get-login-password --region ap-south-1 | sudo docker login --username AWS --password-stdin <ACCT>.dkr.ecr.ap-south-1.amazonaws.com
sudo docker run -d --name paylens --restart unless-stopped -p 80:8000 \
  -e CORS_ORIGINS="https://your-domain" \
  -e LLM_API_KEY="$(aws secretsmanager get-secret-value --secret-id paylens/llm-api-key --query SecretString --output text)" \
  <ACCT>.dkr.ecr.ap-south-1.amazonaws.com/paylens:latest
```

Put Caddy or an ALB in front for TLS. Good enough for an assignment demo.

---

## Sizing

One container = one uvicorn worker, deliberately. The L0/L2 caches are in-process,
so extra workers each keep a separate copy and *cut* the hit rate. Scale with tasks.

| | vCPU | Memory | Notes |
|---|---|---|---|
| Slim | 0.5 | 1 GB | DuckDB is capped at 256 MB; the rest is Python |
| Full | 1 | 2 GB | ONNX runtime wants headroom; 2 GB is comfortable |

Measured locally: import 0.9 s · first query 0.72 s · warm query 0.17 s · full-build
startup ~25 s (one-off ONNX load, done in the lifespan hook, not in a request).

Autoscale on **requests per target** (~30–50 concurrent per task), not CPU — the
workload is I/O-bound on the LLM call whenever one happens.

---

## Before you expose it publicly

Five things, in order of how badly they bite.

1. **`CORS_ORIGINS`.** It defaults to `*` for local dev. Set it to your real origin.
   This is now env-driven (`backend/config.py`); it used to be a hardcoded wildcard.
2. **Set `REQUIRE_AUTH=true`.** It defaults to **false** so the demo is browsable
   without a login — which means an unauthenticated visitor currently gets the
   unrestricted `cxo` role. That is correct for a local demo and completely wrong
   for anything reachable by someone else. The sign-in itself is real (PBKDF2,
   hashed session tokens, role taken from the stored account); the flag only
   decides whether it is compulsory.

3. **Put real authentication in front.** The built-in accounts are seeded demo
   users. The RBAC roles are
   real and enforced in SQL, but the *role comes from the request body* —
   `{"role": "cxo"}` is self-asserted. For anything beyond a demo, terminate auth at
   App Runner/ALB (Cognito, OIDC, or an internal ALB behind your VPN) and map the
   verified identity to `role` + `claims` server-side. `Principal.claims` in
   `backend/core/rbac.py` is exactly where a JWT's claims should land.
4. **`LLM_API_KEY` in Secrets Manager**, never a plaintext env var. The app degrades
   gracefully without a key, so there is no pressure to cut this corner.
5. **Egress.** The only outbound call is to `openrouter.ai` (and, if you did not bake
   the ONNX model, a model download). In a private subnet you need a NAT gateway or a
   VPC endpoint — or run the slim build with no key and no egress at all.
6. **Rate limiting.** A `/ask` can cost an LLM call. Put WAF rate-based rules or an
   API Gateway usage plan in front before anyone outside your team has the URL.

---

## Observability

`/health` already reports `semantic_model`, `transactions`, `kb_records` and
`llm_configured` — alarm on `status != "ok"`, not just on a 200.

Every answer carries a `trace` with `cache_tier`, `llm_calls`, `healed`,
`guard_first_try` and per-step `step_ms`. Emit those as EMF metrics and you get the
dashboard almost free. The four worth alarming on:

| Metric | Why |
|---|---|
| `guard_first_try == false` rate | the planner is generating invalid SQL |
| `healed == true` rate | correlates with answers you should trust less (−0.25 confidence) |
| `cache_tier == cold` rate | the semantic cache is not earning its keep |
| `llm_calls` per request | budget is capped at 4; a rising mean means cost drift |

Logs are stdout JSON-ish via `logging` → CloudWatch, no agent needed.

---

## Updating the data

The dataset is a build artifact, so **new data = new image**. That is the point: the
`docker build` runs `data/generate.py` *and* `data/verify.py` in the same layer, so a
build fails rather than shipping a dataset whose NPCI calibration has drifted.

To point at a real warehouse instead, replace `backend/core/db.py` — it is the only
module that opens a connection. Everything above it speaks SQL that
`sqlglot` already parses for the `duckdb` dialect; moving to Postgres or Athena is a
dialect change plus a connection pool, not a redesign. If you do, revisit the
`EXPLAIN` cost parser in `guard.py` (`_c8_explain`), which reads DuckDB's
`~N rows` format.

---

## Artifacts in this repo

| File | What |
|---|---|
| `Dockerfile` | Multi-stage; `--build-arg WITH_KB=0` for the slim build |
| `.dockerignore` | Excludes local data so the image never ships a dev machine's copy |
| `requirements-runtime.txt` | Serving deps only (no pandas/pyarrow/numpy) |
| `requirements-kb.txt` | Optional Chroma layer |
| `deploy/push-ecr.sh` | Build + push, pinned to `linux/amd64` |
| `deploy/apprunner.yaml` | App Runner config |
| `deploy/ecs-task-definition.json` | Fargate task definition |

**Note:** Docker is not installed in this WSL session, so the Dockerfile is written
and its manifests are syntax-validated, but the image has not been built here. Build
it once locally before you push.
