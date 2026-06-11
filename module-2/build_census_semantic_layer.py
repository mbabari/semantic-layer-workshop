"""Build a semantic layer over the Census Bureau ACS public dataset.

Why census_bureau_acs: it has 278 near-identical tables (14 geography levels x
year x survey span -- e.g. county_2018_5yr, cbsa_2013_3yr, blockgroup_2017_5yr)
and ~252 cryptic demographic columns each (male_22_to_24, income_per_capita,
...). The full schema is ~475K tokens -- it does NOT fit in a 128K-context model.
A naive Text2SQL agent literally cannot paste this schema; it must scan metadata
repeatedly and still picks the wrong geography/year. This is the scenario where a
semantic layer is not an optimization but a requirement.

Stages (re-runnable), all writing into a dedicated `census` Neo4j database:
  1. base         -- Database/Schema/Table/Column from INFORMATION_SCHEMA
                     (value sampling skipped -- it would scan the tables)
  2. descriptions -- deterministic, disambiguating Table descriptions parsed from
                     the <geography>_<year>_<span> naming convention (no LLM loop)
  3. embeddings   -- vector + full-text embeddings on Schema/Table for retrieval

Usage (from repo root):
    ./.venv/bin/python module-2/build_census_semantic_layer.py
    ./.venv/bin/python module-2/build_census_semantic_layer.py --only embeddings
"""

import argparse
import os
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery
from neo4j import Driver, GraphDatabase
from openai import OpenAI

from neocarta.connectors.bigquery import BigQuerySchemaConnector
from neocarta.enrichment.embeddings import OpenAIEmbeddingsConnector

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

SOURCE_PROJECT = os.getenv("CENSUS_SOURCE_PROJECT", "bigquery-public-data")
SOURCE_DATASET = os.getenv("CENSUS_SOURCE_DATASET", "census_bureau_acs")
NEO4J_DATABASE = os.getenv("CENSUS_NEO4J_DATABASE", "census")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))
# Table-level retrieval disambiguates the 278 look-alike tables; column-level
# embeddings let the agent retrieve only the few relevant columns out of ~252 per
# table (the key to token savings on wide schemas). Override via env if needed.
EMBEDDING_NODE_LABELS = [
    s.strip()
    for s in os.getenv("EMBEDDING_NODE_LABELS", "Schema,Table,Column").split(",")
    if s.strip()
]

# Geography phrases are front-loaded and synonym-rich so the embedding/full-text
# signal discriminates geography (the shared measures boilerplate is kept short on
# purpose, otherwise it dominates and retrieval confuses geography levels).
GEO_DESCRIPTIONS = {
    "blockgroup": "Census block groups. The smallest census geography, below census tract; neighborhood-level small areas.",
    "cbsa": "Core-Based Statistical Areas (CBSA): metropolitan and micropolitan statistical areas; metro areas, MSA, metropolitan regions.",
    "censustract": "Census tracts: small subdivisions of a county; neighborhood-level areas.",
    "congressionaldistrict": "Congressional districts: U.S. House of Representatives voting districts / electoral districts.",
    "county": "Counties and county-equivalents.",
    "place": "Census places: incorporated cities, towns, and villages; municipalities.",
    "puma": "Public Use Microdata Areas (PUMA).",
    "schooldistrictelementary": "Elementary school districts.",
    "schooldistrictsecondary": "Secondary school districts.",
    "schooldistrictunified": "Unified school districts.",
    "state": "U.S. states and state-equivalents.",
    "zcta": "ZIP Code Tabulation Areas (ZCTA): ZIP code areas / postal code areas.",
    "zcta5": "5-digit ZIP Code Tabulation Areas (ZCTA5): ZIP code areas / postal code areas.",
    "zip_codes": "ZIP codes: postal ZIP code areas.",
}

SCHEMA_DESCRIPTION = (
    "U.S. Census Bureau American Community Survey (ACS) demographic, social, "
    "economic, and housing estimates across many geography levels and survey years."
)

_TABLE_RE = re.compile(r"^(?P<geo>.+?)_(?P<year>\d{4})_(?P<span>\dyr)$")


def _driver() -> Driver:
    drv = GraphDatabase.driver(
        os.getenv("NEO4J_URI"),
        auth=(os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")),
    )
    drv.verify_connectivity()
    return drv


def ensure_database(neo4j_driver: Driver) -> None:
    """Create the dedicated census database if the server supports multi-db."""
    try:
        with neo4j_driver.session(database="system") as session:
            session.run(f"CREATE DATABASE {NEO4J_DATABASE} IF NOT EXISTS").consume()
        print(f"[db] ensured database `{NEO4J_DATABASE}`.")
    except Exception as e:  # community edition / no privilege -> reuse existing db
        print(f"[db] could not create database `{NEO4J_DATABASE}` ({e}). "
              "Ensure it exists or set CENSUS_NEO4J_DATABASE.")


def build_base_graph(neo4j_driver: Driver, bigquery_client: bigquery.Client) -> None:
    print(f"[base] extracting schema for {SOURCE_PROJECT}.{SOURCE_DATASET} ...")
    connector = BigQuerySchemaConnector(
        client=bigquery_client,
        project_id=SOURCE_PROJECT,
        dataset_id=SOURCE_DATASET,
        neo4j_driver=neo4j_driver,
        database_name=NEO4J_DATABASE,
    )
    connector.extractor.project_id = SOURCE_PROJECT
    connector.extractor._cache["database_info"] = pd.DataFrame([{"project_id": SOURCE_PROJECT}])
    connector.extractor._cache["schema_info"] = pd.DataFrame(
        [{"project_id": SOURCE_PROJECT, "dataset_id": SOURCE_DATASET, "description": SCHEMA_DESCRIPTION}]
    )
    connector.extractor.extract_table_info()
    connector.extractor.extract_column_info()
    connector.extractor.extract_column_references_info()
    # Skip extract_column_unique_values_for_all_tables (would scan the tables).

    print(
        f"[base] tables={len(connector.extractor.table_info)} "
        f"columns={len(connector.extractor.column_info)}"
    )
    connector.transform_metadata()
    connector.load_metadata()
    connector.loader.upsert_neocarta_graph_node()
    print("[base] loaded into Neo4j.")


def _describe(table: str) -> str:
    m = _TABLE_RE.match(table)
    if not m:
        return f"ACS table `{table}`."
    geo = GEO_DESCRIPTIONS.get(m["geo"], m["geo"].replace("_", " "))
    span = {"1yr": "1-year", "3yr": "3-year", "5yr": "5-year"}.get(m["span"], m["span"])
    # Description = ONLY the distinctive geography phrase + year + span. The ACS
    # measures are identical across all 278 tables, so including them only dilutes
    # the embedding and makes geography levels look alike. Keep the signal pure.
    return f"{geo} ACS {m['year']} {span} estimates ({m['geo']}, year {m['year']}, {span})."


def generate_descriptions(neo4j_driver: Driver) -> None:
    """Set deterministic, disambiguating descriptions on every Table node."""
    with neo4j_driver.session(database=NEO4J_DATABASE) as session:
        tables = [
            r["name"]
            for r in session.run(
                "MATCH (s:Schema {name: $schema})-[:HAS_TABLE]->(t:Table) RETURN t.name AS name",
                schema=SOURCE_DATASET,
            )
        ]
        items = [{"table": t, "desc": _describe(t)} for t in tables]
        session.run(
            """
            UNWIND $items AS item
            MATCH (t:Table {name: item.table})
            SET t.description = item.desc, t.embedding = NULL
            """,
            items=items,
        )
    print(f"[desc] set descriptions on {len(items)} tables (embeddings cleared for re-embed).")


def generate_embeddings(neo4j_driver: Driver, openai_client: OpenAI) -> None:
    print(f"[embed] embedding {EMBEDDING_NODE_LABELS} ({EMBEDDING_MODEL}, dim={EMBEDDING_DIMENSIONS}) ...")
    OpenAIEmbeddingsConnector(
        client=openai_client,
        embedding_model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        neo4j_driver=neo4j_driver,
        database_name=NEO4J_DATABASE,
    ).run(node_labels=EMBEDDING_NODE_LABELS)


def report(neo4j_driver: Driver) -> None:
    with neo4j_driver.session(database=NEO4J_DATABASE) as session:
        counts = session.run(
            """
            MATCH (s:Schema {name: $schema})
            OPTIONAL MATCH (s)-[:HAS_TABLE]->(t:Table)
            OPTIONAL MATCH (t)-[:HAS_COLUMN]->(c:Column)
            RETURN count(DISTINCT t) AS tables, count(c) AS columns,
                   count(DISTINCT CASE WHEN t.description IS NOT NULL THEN t END) AS described,
                   count(DISTINCT CASE WHEN t.embedding IS NOT NULL THEN t END) AS embedded
            """,
            schema=SOURCE_DATASET,
        ).single()
        print(
            f"\n[report] tables={counts['tables']} columns={counts['columns']} "
            f"described={counts['described']} embedded={counts['embedded']}"
        )
        print("[report] sample tables:")
        for r in session.run(
            """
            MATCH (s:Schema {name: $schema})-[:HAS_TABLE]->(t:Table)
            RETURN t.name AS table, t.description AS desc ORDER BY table LIMIT 5
            """,
            schema=SOURCE_DATASET,
        ):
            print(f"  {r['table']:28} {(r['desc'] or '')[:80]}")


STAGES = ("base", "descriptions", "embeddings")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", choices=STAGES, default=list(STAGES))
    args = parser.parse_args()
    stages = set(args.only)

    neo4j_driver = _driver()
    bigquery_client = bigquery.Client(project=os.getenv("GCP_PROJECT_ID"))
    openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    print(f"Target Neo4j database: {NEO4J_DATABASE}\n")

    ensure_database(neo4j_driver)
    if "base" in stages:
        build_base_graph(neo4j_driver, bigquery_client)
    if "descriptions" in stages:
        generate_descriptions(neo4j_driver)
    if "embeddings" in stages:
        generate_embeddings(neo4j_driver, openai_client)

    report(neo4j_driver)
    neo4j_driver.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
