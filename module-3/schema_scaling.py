"""Measure how schema-discovery token cost scales with the number of tables.

This isolates the variable that actually drives the WITH-vs-WITHOUT-neocarta token
difference at scale: a brute-force agent must read the *table inventory* (grows with
table count) and *describe candidate tables* before it can write SQL, whereas neocarta
returns a roughly constant top-k of relevant tables regardless of schema size.

We tokenize real BigQuery INFORMATION_SCHEMA metadata with the o200k_base tokenizer
(used by gpt-4o / gpt-4.1) across datasets of increasing size.

    uv run module-3/schema_scaling.py
"""

import os

import tiktoken
from dotenv import load_dotenv
from google.cloud import bigquery

load_dotenv()
ENC = tiktoken.get_encoding("o200k_base")
TOK = lambda s: len(ENC.encode(s))  # noqa: E731

# (project, dataset) ordered roughly by table count. acme_corp is in the user's project.
DATASETS = [
    ("bigquery-public-data", "thelook_ecommerce"),
    ("bigquery-public-data", "cms_medicare"),
    ("bigquery-public-data", "new_york_taxi_trips"),
    (os.getenv("GCP_PROJECT_ID"), "acme_corp"),
    ("bigquery-public-data", "ga4_obfuscated_sample_ecommerce"),
    ("bigquery-public-data", "noaa_gsod"),
    ("bigquery-public-data", "patents_view"),
    ("bigquery-public-data", "census_bureau_acs"),
    ("bigquery-public-data", "google_analytics_sample"),
]

# neocarta hybrid/vector search returns a fixed number of relevant tables regardless
# of total schema size. Empirically ~this many tables of context per retrieval.
NEOCARTA_TOPK = 5
# A brute-force agent typically describes a few candidate tables per question.
BRUTE_TABLES_DESCRIBED = 3


def main() -> None:
    client = bigquery.Client(project=os.getenv("GCP_PROJECT_ID"))
    rows = []
    for project, ds in DATASETS:
        q = f"""
        SELECT table_name, column_name, data_type
        FROM `{project}.{ds}.INFORMATION_SCHEMA.COLUMNS`
        ORDER BY table_name, ordinal_position
        """
        try:
            df = list(client.query(q).result())
        except Exception as e:  # noqa: BLE001
            print(f"  skip {ds}: {str(e)[:70]}")
            continue

        # Per-table schema text, like what get_table_info / a DDL dump returns.
        per_table: dict[str, list[str]] = {}
        for r in df:
            per_table.setdefault(r["table_name"], []).append(f"{r['column_name']} {r['data_type']}")
        n = len(per_table)
        table_names = list(per_table.keys())

        inventory_tok = TOK(", ".join(table_names))                       # list_table_ids
        table_toks = sorted(TOK(f"{t}({', '.join(cols)})") for t, cols in per_table.items())
        full_tok = sum(table_toks)                                        # describe everything
        median_table = table_toks[len(table_toks) // 2]

        rows.append((ds, n, inventory_tok, full_tok, median_table))

    # ---- raw measured metadata sizes ----
    print(f"\n{'dataset':<34}{'tables':>7}{'inventory':>11}{'full schema':>13}{'median tbl':>11}")
    print("-" * 76)
    for ds, n, inv, full, med in rows:
        print(f"{ds:<34}{n:>7}{inv:>11,}{full:>13,}{med:>11,}")

    # ---- model the two strategies (input tokens to "know enough to query") ----
    print(f"\nModel: brute-force = inventory + {BRUTE_TABLES_DESCRIBED} tables described;  "
          f"neocarta = {NEOCARTA_TOPK} relevant tables (constant)")
    print(f"\n{'dataset':<34}{'tables':>7}{'brute-force':>13}{'neocarta':>11}{'savings':>9}")
    print("-" * 74)
    for ds, n, inv, full, med in rows:
        brute = inv + BRUTE_TABLES_DESCRIBED * med
        neo = NEOCARTA_TOPK * med            # constant in N: top-k relevant tables only
        save = 100 * (brute - neo) / brute if brute else 0
        flag = "  <- neocarta wins" if neo < brute else "  <- brute wins"
        print(f"{ds:<34}{n:>7}{brute:>13,}{neo:>11,}{save:>8.0f}%{flag}")

    print("\nNotes:")
    print("* 'inventory' = tokens just to list every table name (what list_table_ids returns)")
    print("  cold, every question, and it grows with table count.")
    print("* neocarta retrieval is ~flat: top-k relevant tables regardless of schema size.")
    print("* This counts only schema-discovery tokens, not the question/answer/SQL, and")
    print("  ignores retries (which inflate the brute-force path further).")


if __name__ == "__main__":
    main()
