"""Build the Spider 2.0 semantic-layer metadata graph in Neo4j with Neocarta.

Mirrors the ETL pipeline in `2a_build_metadata_graph.ipynb`, but targets the
curated `spider2` BigQuery dataset (created from bigquery-public-data) and writes
into the dedicated `spider2` Neo4j database — without disturbing the ACME setup
configured in `.env` (NEO4J_DATABASE / BIGQUERY_DATASET_ID).

Run from the repo root:

    ./.venv/bin/python module-2/build_spider2_metadata_graph.py

Connection details (NEO4J_URI/USERNAME/PASSWORD, GCP_PROJECT_ID, OPENAI_API_KEY)
are read from `.env`. The BigQuery dataset and Neo4j database default to `spider2`
and can be overridden with SPIDER2_BQ_DATASET / SPIDER2_NEO4J_DATABASE.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery
from neo4j import Driver, GraphDatabase
from openai import OpenAI

from neocarta.connectors.bigquery import BigQuerySchemaConnector
from neocarta.enrichment.embeddings import OpenAIEmbeddingsConnector

# Load .env from the repo root regardless of where the script is invoked.
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

# Targets — independent of the ACME values in .env.
BQ_DATASET = os.getenv("SPIDER2_BQ_DATASET", "spider2")
NEO4J_DATABASE = os.getenv("SPIDER2_NEO4J_DATABASE", "spider2")

# Must match what the agent's MCP server uses for vector search (see 3_agent.py).
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))
EMBEDDING_NODE_LABELS = ["Schema", "Table", "Column"]


def main() -> None:
    """Run extract -> transform -> load -> embeddings for the spider2 dataset."""
    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id:
        raise SystemExit("GCP_PROJECT_ID is not set in .env")

    neo4j_driver = GraphDatabase.driver(
        os.getenv("NEO4J_URI"),
        auth=(os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")),
    )
    neo4j_driver.verify_connectivity()
    print(f"Neo4j connected -> database '{NEO4J_DATABASE}'")

    bigquery_client = bigquery.Client(project=project_id)
    print(f"BigQuery connected -> {project_id}.{BQ_DATASET}")

    embedding_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    connector = BigQuerySchemaConnector(
        client=bigquery_client,
        project_id=project_id,
        dataset_id=BQ_DATASET,
        neo4j_driver=neo4j_driver,
        database_name=NEO4J_DATABASE,
    )

    print("\n[1/4] Extracting metadata from BigQuery INFORMATION_SCHEMA...")
    connector.extract_metadata()
    print(
        f"  tables={len(connector.extractor.table_info)} "
        f"columns={len(connector.extractor.column_info)} "
        f"fk_refs={len(connector.extractor.column_references_info)}"
    )

    print("\n[2/4] Transforming into graph models...")
    connector.transform_metadata()

    print("\n[3/4] Loading nodes/relationships into Neo4j...")
    connector.load_metadata()
    connector.loader.upsert_neocarta_graph_node()

    print(f"\n[4/4] Generating embeddings ({EMBEDDING_MODEL}, dim={EMBEDDING_DIMENSIONS})...")
    OpenAIEmbeddingsConnector(
        client=embedding_client,
        embedding_model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        neo4j_driver=neo4j_driver,
        database_name=NEO4J_DATABASE,
    ).run(node_labels=EMBEDDING_NODE_LABELS)

    _report(neo4j_driver)
    neo4j_driver.close()
    print("\nDone.")


def _report(neo4j_driver: Driver) -> None:
    """Print node counts, FK edges, and embedding coverage for verification."""
    with neo4j_driver.session(database=NEO4J_DATABASE) as session:
        print("\nNode counts:")
        for record in session.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count ORDER BY count"
        ):
            print(f"  - {record['label']}: {record['count']}")

        fk = session.run(
            "MATCH ()-[r:REFERENCES]->() RETURN count(r) AS c"
        ).single()["c"]
        print(f"\nForeign key (REFERENCES) edges: {fk}")

        print("\nEmbedding coverage:")
        for record in session.run(
            """
            MATCH (n)
            WHERE labels(n)[0] IN $labels
            RETURN labels(n)[0] AS label,
                   count(n) AS total,
                   count(n.embedding) AS embedded
            ORDER BY label
            """,
            labels=EMBEDDING_NODE_LABELS,
        ):
            mark = "OK" if record["total"] == record["embedded"] else "!!"
            print(
                f"  [{mark}] {record['label']}: "
                f"{record['embedded']}/{record['total']} embedded"
            )


if __name__ == "__main__":
    main()
