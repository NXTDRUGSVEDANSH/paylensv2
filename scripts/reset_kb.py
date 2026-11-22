"""
Reset the knowledge stores so a demo run is reproducible.

    python -m scripts.reset_kb            drop learned v2 records, rebuild the glossary
    python -m scripts.reset_kb --all      drop EVERY few-shot record as well

Two stores, and they are reset differently on purpose.

`paylens_sql` holds question->SQL few-shots. v2 writes one back after a clean
run, so it grows with use; dropping those makes a demo start from the same
place twice. Records written by the retired v1 engine are left alone unless
--all is passed: they are shaped against fact_transactions, which v2 blocks, so
v2 labels them `legacy_schema` and shows them as shape rather than copying them.
They can no longer be regenerated — the v1 seeder went with the v1 compiler —
so deleting them is one-way.

`paylens_knowledge` holds the definitions and is rebuilt from source every time,
because it is generated from views.yaml rather than learned.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.kb import store                        # noqa: E402
from paylens2.core import retrieval                 # noqa: E402
from paylens2.knowledge import build as kbuild      # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="also drop few-shots written by the retired v1 engine "
                         "— one-way, they cannot be regenerated")
    args = ap.parse_args()

    if not store.available():
        print("knowledge base unavailable; nothing to reset")
        return

    n = retrieval.purge_v2(stale_only=False)
    print(f"dropped {n} v2 few-shot record(s)")

    if args.all:
        n = store.reset(keep_seeds=False)
        print(f"dropped {n} remaining record(s), seeds included")

    print(f"{kbuild.build()} knowledge chunks rebuilt from views.yaml, "
          f"playbooks.yaml and industry.yaml")
    print(f"few-shot corpus now holds {store.get_kb().count()} record(s)")


if __name__ == "__main__":
    main()
