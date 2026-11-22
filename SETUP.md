# Setup and commands

The README explains how PayLens works. This file is the operational half.

## Install and run

```bash
# Python 3.12 — 3.14 has no onnxruntime wheels for chromadb
python -m venv ~/.venvs/paylens && source ~/.venvs/paylens/bin/activate
pip install -r requirements.txt

cp .env.example .env              # add an OpenRouter key
python data/generate.py           # deterministic, seed 42, ~40s
python data/verify.py             # acceptance checks on the generated data
python -m paylens2.schema.build   # creates v_attempts and v_settlements

python -m paylens2.api.app        # http://localhost:8001
```

`GET /v2/health` reports whether the views are built and which model is wired
up. Signing in is optional; it changes the row filter the guard injects into
your SQL, and you can watch it appear in the SQL tab.

## Tests

```bash
pytest -q                       # 240 tests
pytest evaluation/tests -q      # 47 more; no language model or network needed
```

## Evaluation

```bash
python -m evaluation.datasets.bank_build --check    # validates the 400 questions
python -m evaluation.runners.run_own_eval --smoke   # one per bucket, ~90s
python -m evaluation.runners.run_own_eval --pipeline v2,baseline --workers 3
python -m evaluation.runners.run_judge   --run <run_id>   # G-Eval, separately
python docs/build_deck.py --run <run_id>                  # rebuild the report
```

Every formula and what it catches is in [`evaluation/METRICS.md`](evaluation/METRICS.md).

## BIRD

The BIRD track is built and tested but needs the Mini-Dev SQLite databases,
which are distributed only through a Google Drive link on
<https://bird-bench.github.io/>. Extract them so each database sits at
`evaluation/datasets/bird_dbs/<db_id>/<db_id>.sqlite`. The 500 questions and
their gold SQL load from HuggingFace automatically.

```bash
python -m evaluation.runners.run_bird_eval --smoke
python -m evaluation.runners.run_bird_eval --pipeline v2,baseline --limit 500
```

The runner preflights and evaluates only the databases that are present, so it
is correct at none, three or all eleven of them.

## API

| Route | What it does |
|---|---|
| `POST /v2/ask` | a question in, the full answer out |
| `POST /v2/ask/stream` | the same answer, preceded by the pipeline's own stage events |
| `GET /v2/health` | views built? model configured? which tables are allowed? |
| `GET /v2/schema` | the exact schema card the authoring call receives |
| `GET /v2/catalogue` | the advisory entries the authoring call may select from |

The role comes from the authenticated session and is never read from the
request body, so a caller cannot assert its own identity.
