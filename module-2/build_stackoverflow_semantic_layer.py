"""Build a rich semantic layer over the StackOverflow public dataset.

Why StackOverflow: bigquery-public-data.stackoverflow has 16 tables, 10 of which
are near-identical `posts_*` tables (same 20 columns), with NO column descriptions
and NO primary/foreign-key constraints. A naive Text2SQL agent has to dump all
~270 columns, can't tell `posts_questions` from `posts_answers` from
`stackoverflow_posts`, and has no join paths -- so it re-scans the schema and
burns tokens. This is the scenario the semantic layer is meant to win.

This script builds the layer in re-runnable stages, all writing into the `spider2`
Neo4j database (additive; does not touch the ACME `.env` config):

  1. base graph   -- Database/Schema/Table/Column from INFORMATION_SCHEMA
                     (value sampling is skipped: it would scan ~254 GB)
  2. descriptions -- LLM-generated table + column descriptions (none exist upstream)
  3. fk paths     -- known StackOverflow join paths as :REFERENCES edges
  4. embeddings   -- vector embeddings on Schema/Table/Column for semantic search

Usage (from repo root):
    ./.venv/bin/python module-2/build_stackoverflow_semantic_layer.py
    ./.venv/bin/python module-2/build_stackoverflow_semantic_layer.py --only descriptions embeddings
"""

import argparse
import json
import os
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery
from neo4j import Driver, GraphDatabase
from openai import OpenAI

from neocarta.connectors.bigquery import BigQuerySchemaConnector
from neocarta.enrichment.embeddings import OpenAIEmbeddingsConnector

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

SOURCE_PROJECT = os.getenv("STACKOVERFLOW_SOURCE_PROJECT", "bigquery-public-data")
SOURCE_DATASET = os.getenv("STACKOVERFLOW_SOURCE_DATASET", "stackoverflow")
NEO4J_DATABASE = os.getenv("SPIDER2_NEO4J_DATABASE", "spider2")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))
EMBEDDING_NODE_LABELS = ["Schema", "Table", "Column"]
DESCRIPTION_MODEL = os.getenv("DESCRIPTION_MODEL", "gpt-4o-mini")

# Well-known StackOverflow relationships (no FK constraints exist upstream).
# (from_table, from_column, to_table, to_column)
STACKOVERFLOW_FKS = [
    ("posts_questions", "owner_user_id", "users", "id"),
    ("posts_questions", "last_editor_user_id", "users", "id"),
    ("posts_questions", "accepted_answer_id", "posts_answers", "id"),
    ("posts_answers", "owner_user_id", "users", "id"),
    ("posts_answers", "last_editor_user_id", "users", "id"),
    ("posts_answers", "parent_id", "posts_questions", "id"),
    ("stackoverflow_posts", "owner_user_id", "users", "id"),
    ("comments", "user_id", "users", "id"),
    ("comments", "post_id", "posts_questions", "id"),
    ("votes", "post_id", "posts_questions", "id"),
    ("badges", "user_id", "users", "id"),
    ("post_history", "user_id", "users", "id"),
    ("post_history", "post_id", "posts_questions", "id"),
    ("post_links", "post_id", "posts_questions", "id"),
    ("post_links", "related_post_id", "posts_questions", "id"),
]

SCHEMA_DESCRIPTION = (
    "Stack Overflow public dataset: questions, answers, comments, votes, badges, "
    "users, tags, and post edit history from the Stack Overflow Q&A site."
)


def _driver() -> Driver:
    drv = GraphDatabase.driver(
        os.getenv("NEO4J_URI"),
        auth=(os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")),
    )
    drv.verify_connectivity()
    return drv


def build_base_graph(neo4j_driver: Driver, bigquery_client: bigquery.Client) -> None:
    """Load Database/Schema/Table/Column from INFORMATION_SCHEMA (no value sampling)."""
    print(f"[base] extracting schema for {SOURCE_PROJECT}.{SOURCE_DATASET} ...")
    connector = BigQuerySchemaConnector(
        client=bigquery_client,
        project_id=SOURCE_PROJECT,
        dataset_id=SOURCE_DATASET,
        neo4j_driver=neo4j_driver,
        database_name=NEO4J_DATABASE,
    )
    # The client's billing project runs the jobs, but INFORMATION_SCHEMA FROM clauses
    # must reference the public source project.
    connector.extractor.project_id = SOURCE_PROJECT

    # Seed database/schema info directly to avoid cross-project project-level
    # INFORMATION_SCHEMA.SCHEMATA_OPTIONS lookups.
    connector.extractor._cache["database_info"] = pd.DataFrame(
        [{"project_id": SOURCE_PROJECT}]
    )
    connector.extractor._cache["schema_info"] = pd.DataFrame(
        [{"project_id": SOURCE_PROJECT, "dataset_id": SOURCE_DATASET, "description": SCHEMA_DESCRIPTION}]
    )
    # Dataset-qualified INFORMATION_SCHEMA reads (metadata only, ~0 bytes scanned).
    connector.extractor.extract_table_info()
    connector.extractor.extract_column_info()
    connector.extractor.extract_column_references_info()
    # Intentionally skip extract_column_unique_values_for_all_tables (full-table scans).

    print(
        f"[base] tables={len(connector.extractor.table_info)} "
        f"columns={len(connector.extractor.column_info)}"
    )
    connector.transform_metadata()
    connector.load_metadata()
    connector.loader.upsert_neocarta_graph_node()
    print("[base] loaded into Neo4j.")


def _fetch_schema_for_descriptions(neo4j_driver: Driver) -> dict[str, list[str]]:
    """Return {table_name: [column_name, ...]} for the StackOverflow schema."""
    table_names = {fk[0] for fk in STACKOVERFLOW_FKS} | {fk[2] for fk in STACKOVERFLOW_FKS}
    with neo4j_driver.session(database=NEO4J_DATABASE) as session:
        rows = session.run(
            """
            MATCH (s:Schema {name: $schema})-[:HAS_TABLE]->(t:Table)-[:HAS_COLUMN]->(c:Column)
            RETURN t.name AS table, collect(c.name) AS columns
            ORDER BY table
            """,
            schema=SOURCE_DATASET,
        )
        result = {r["table"]: sorted(r["columns"]) for r in rows}
    # Fall back to matching by known table names if the Schema node name differs.
    if not result:
        with neo4j_driver.session(database=NEO4J_DATABASE) as session:
            rows = session.run(
                """
                MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
                WHERE t.name IN $tables
                RETURN t.name AS table, collect(c.name) AS columns
                ORDER BY table
                """,
                tables=list(table_names),
            )
            result = {r["table"]: sorted(r["columns"]) for r in rows}
    return result


def generate_descriptions(neo4j_driver: Driver, openai_client: OpenAI) -> None:
    """Generate and write table + column descriptions (one LLM call per table)."""
    schema = _fetch_schema_for_descriptions(neo4j_driver)
    if not schema:
        print("[desc] no StackOverflow tables found in graph; run base stage first.")
        return

    print(f"[desc] generating descriptions for {len(schema)} tables with {DESCRIPTION_MODEL} ...")
    for table, columns in schema.items():
        prompt = (
            "You are documenting the Stack Overflow public BigQuery dataset for a "
            "data catalog used by a Text2SQL agent. Write concise, disambiguating "
            "descriptions. Make clear how near-identical tables differ (e.g. "
            "posts_questions vs posts_answers vs stackoverflow_posts) and what each "
            "column means / how it joins.\n\n"
            f"Table: {table}\nColumns: {', '.join(columns)}\n\n"
            'Return JSON: {"table_description": "...", '
            '"columns": {"<column_name>": "<description>", ...}}. '
            "Cover every column listed."
        )
        resp = openai_client.chat.completions.create(
            model=DESCRIPTION_MODEL,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        table_desc = data.get("table_description", "")
        col_descs = data.get("columns", {})

        with neo4j_driver.session(database=NEO4J_DATABASE) as session:
            session.run(
                "MATCH (t:Table {name: $table}) SET t.description = $desc",
                table=table,
                desc=table_desc,
            )
            session.run(
                """
                UNWIND $items AS item
                MATCH (t:Table {name: $table})-[:HAS_COLUMN]->(c:Column {name: item.col})
                SET c.description = item.desc
                """,
                table=table,
                items=[{"col": k, "desc": v} for k, v in col_descs.items()],
            )
        print(f"[desc]   {table}: table + {len(col_descs)} columns")


def add_fk_paths(neo4j_driver: Driver) -> None:
    """Create :REFERENCES edges for known StackOverflow relationships."""
    print(f"[fk] adding {len(STACKOVERFLOW_FKS)} join paths ...")
    created = 0
    with neo4j_driver.session(database=NEO4J_DATABASE) as session:
        for from_t, from_c, to_t, to_c in STACKOVERFLOW_FKS:
            summary = session.run(
                """
                MATCH (ft:Table {name: $from_t})-[:HAS_COLUMN]->(fc:Column {name: $from_c})
                MATCH (tt:Table {name: $to_t})-[:HAS_COLUMN]->(tc:Column {name: $to_c})
                MERGE (fc)-[r:REFERENCES]->(tc)
                SET fc.is_foreign_key = true
                RETURN count(r) AS c
                """,
                from_t=from_t, from_c=from_c, to_t=to_t, to_c=to_c,
            ).single()
            if summary and summary["c"]:
                created += 1
            else:
                print(f"[fk]   skipped (columns not found): {from_t}.{from_c} -> {to_t}.{to_c}")
    print(f"[fk] ensured {created}/{len(STACKOVERFLOW_FKS)} REFERENCES edges.")


def generate_embeddings(neo4j_driver: Driver, openai_client: OpenAI) -> None:
    """Embed Schema/Table/Column nodes that now carry descriptions."""
    print(f"[embed] embedding {EMBEDDING_NODE_LABELS} ({EMBEDDING_MODEL}, dim={EMBEDDING_DIMENSIONS}) ...")
    OpenAIEmbeddingsConnector(
        client=openai_client,
        embedding_model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        neo4j_driver=neo4j_driver,
        database_name=NEO4J_DATABASE,
    ).run(node_labels=EMBEDDING_NODE_LABELS)


def report(neo4j_driver: Driver) -> None:
    """Print coverage for the StackOverflow tables in the graph."""
    with neo4j_driver.session(database=NEO4J_DATABASE) as session:
        print("\nStackOverflow tables in graph:")
        for r in session.run(
            """
            MATCH (s:Schema {name: $schema})-[:HAS_TABLE]->(t:Table)
            OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)
            RETURN t.name AS table,
                   t.description IS NOT NULL AS has_desc,
                   count(c) AS cols,
                   count(c.embedding) AS embedded
            ORDER BY table
            """,
            schema=SOURCE_DATASET,
        ):
            print(
                f"  {r['table']:32} desc={r['has_desc']!s:5} "
                f"cols={r['cols']:3} embedded={r['embedded']:3}"
            )
        fk = session.run(
            """
            MATCH (s:Schema {name: $schema})-[:HAS_TABLE]->(:Table)-[:HAS_COLUMN]->(:Column)-[r:REFERENCES]->()
            RETURN count(r) AS c
            """,
            schema=SOURCE_DATASET,
        ).single()["c"]
        print(f"\nStackOverflow REFERENCES edges: {fk}")


STAGES = ("base", "descriptions", "fk", "embeddings")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", nargs="+", choices=STAGES, default=list(STAGES),
        help="Run only the named stages (default: all).",
    )
    args = parser.parse_args()
    stages = set(args.only)

    neo4j_driver = _driver()
    bigquery_client = bigquery.Client(project=os.getenv("GCP_PROJECT_ID"))
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    print(f"Target Neo4j database: {NEO4J_DATABASE}\n")

    if "base" in stages:
        build_base_graph(neo4j_driver, bigquery_client)
    if "descriptions" in stages:
        generate_descriptions(neo4j_driver, openai_client)
    if "fk" in stages:
        add_fk_paths(neo4j_driver)
    if "embeddings" in stages:
        generate_embeddings(neo4j_driver, openai_client)

    report(neo4j_driver)
    neo4j_driver.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
