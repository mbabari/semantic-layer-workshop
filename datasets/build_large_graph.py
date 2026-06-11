"""Build a neocarta metadata graph over the large acme_dw dataset (135 tables)
into a dedicated Neo4j database, then embed Schema/Table/Column for semantic search.
"""

import os
import time

from dotenv import load_dotenv
from google.cloud import bigquery
from neo4j import GraphDatabase
from openai import OpenAI

from neocarta.connectors.bigquery import BigQuerySchemaConnector
from neocarta.enrichment.embeddings import OpenAIEmbeddingsConnector
from neocarta.enums import NodeLabel

load_dotenv()

PROJECT = os.getenv("GCP_PROJECT_ID")
DATASET = "acme_dw"
DB = "acmedw"  # Neo4j db names disallow underscores

driver = GraphDatabase.driver(
    os.getenv("NEO4J_URI"),
    auth=(os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")),
)

# 1. fresh database
with driver.session(database="system") as s:
    s.run(f"CREATE OR REPLACE DATABASE {DB}").consume()
print(f"Created database {DB}; waiting for ONLINE...")
for _ in range(30):
    with driver.session(database="system") as s:
        rec = s.run("SHOW DATABASE $db YIELD currentStatus", db=DB).single()
    if rec and rec["currentStatus"] == "online":
        break
    time.sleep(1)
print("  online.")

# 2. load schema graph from BigQuery INFORMATION_SCHEMA
bq = bigquery.Client(project=PROJECT)
BigQuerySchemaConnector(
    client=bq, project_id=PROJECT, dataset_id=DATASET,
    neo4j_driver=driver, database_name=DB,
).run()

with driver.session(database=DB) as s:
    t = s.run("MATCH (t:Table) RETURN count(t) AS c").single()["c"]
    c = s.run("MATCH (c:Column) RETURN count(c) AS c").single()["c"]
print(f"Loaded graph: {t} tables, {c} columns")

# 3. embeddings + vector indexes for semantic search
print("Generating embeddings (Schema, Table, Column)...")
OpenAIEmbeddingsConnector(
    neo4j_driver=driver,
    client=OpenAI(api_key=os.getenv("OPENAI_API_KEY")),
    embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
    dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "768")),
    database_name=DB,
).run(node_labels=[NodeLabel.SCHEMA, NodeLabel.TABLE, NodeLabel.COLUMN])

with driver.session(database=DB) as s:
    emb = s.run("MATCH (n) WHERE n.embedding IS NOT NULL RETURN count(n) AS c").single()["c"]
    idx = [(r["name"], r["type"]) for r in s.run(
        "SHOW INDEXES YIELD name,type WHERE type IN ['VECTOR','FULLTEXT'] RETURN name,type")]
print(f"Embedded {emb} nodes. Search indexes: {idx}")
driver.close()
print("DONE.")
