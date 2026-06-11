# Troubleshooting Guide

Common errors encountered during the workshop and how to fix them.

---

## 1. Neo4j Connection Errors

### `ServiceUnavailable: Unable to retrieve routing information`

**Cause:** Wrong URI format or Neo4j not reachable.

**Fix:**
- AuraDB URIs must use `neo4j+s://` prefix (not `bolt://`)
  ```
  # Correct for AuraDB:
  NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io

  # Correct for local Desktop:
  NEO4J_URI=bolt://localhost:7687
  ```
- For AuraDB: check that your IP isn't blocked. AuraDB Free allows all IPs by default.
- Ping the hostname: `ping xxxxxxxx.databases.neo4j.io` (should resolve)

### `AuthError: The client is unauthorized due to authentication failure`

**Cause:** Wrong username or password in `.env`.

**Fix:**
- For AuraDB: the password was shown once at instance creation. If lost, go to AuraDB console → instance → "Reset password"
- Default username is always `neo4j` for both AuraDB and Desktop

### `ClientError: The database does not exist`

**Cause:** `NEO4J_DATABASE` set to a database name that doesn't exist.

**Fix:**
- For AuraDB Free: set `NEO4J_DATABASE=neo4j` (only one database is available)
- For Desktop: open Neo4j Browser, run `SHOW DATABASES` to see available databases

---

## 2. BigQuery Auth Errors

### `google.auth.exceptions.DefaultCredentialsError`

**Cause:** Application Default Credentials not configured.

**Fix:**
```bash
gcloud auth application-default login
```
Then re-run the failing command.

### `google.api_core.exceptions.Forbidden: 403 Access Denied`

**Cause:** The authenticated GCP account doesn't have BigQuery access to the dataset.

**Fix:**
- Confirm the account used in `gcloud auth application-default login` has BigQuery Data Viewer + BigQuery Job User roles
- Check with: `gcloud auth application-default print-access-token` (should return a token, not an error)
- Verify project: `gcloud config get-value project`

### `google.api_core.exceptions.NotFound: 404 Dataset not found`

**Cause:** `BIGQUERY_DATASET_ID` in `.env` doesn't exist in the project, or `GCP_PROJECT_ID` is wrong.

**Fix:**
- Run notebook `2a_build_metadata_graph.ipynb` to load the Acme BigQuery dataset
- Verify dataset exists: `bq ls --project_id=YOUR_PROJECT_ID`
- Check `.env` values match your project and dataset

---

## 3. BigQuery MCP Server Not Responding

### `httpx.ConnectError` or `403 Forbidden` on BigQuery MCP

**Cause:** BigQuery MCP API not enabled or wrong project ID.

**Fix:**
1. Enable the API:
   ```bash
   gcloud beta services mcp enable bigquery.googleapis.com --project=YOUR_PROJECT_ID
   ```
2. Verify `GCP_PROJECT_ID` in `.env` matches the project where the API is enabled
3. Test with curl:
   ```bash
   curl -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
     https://bigquery.googleapis.com/mcp
   ```
   Expected: a JSON response (not a 403/404)

### `google.auth.exceptions.TransportError`

**Cause:** ADC token expired.

**Fix:**
```bash
gcloud auth application-default login
```

### Still can't reach the remote BigQuery MCP server?

The remote BigQuery MCP API (`https://bigquery.googleapis.com/mcp`) is in beta and
may be unavailable in some regions or projects. As a fallback, run the local
[**MCP Toolbox for Databases**](https://googleapis.github.io/genai-toolbox/) instead — it exposes
an equivalent `execute_sql` tool over a local stdio server, so the agent's tool
allowlist works unchanged.

Replace the `bigquery_mcp_params` block in [`../module-3/3_agent.py`](../module-3/3_agent.py) with a stdio server:

```python
bigquery_mcp_params = {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@toolbox-sdk/server", "--prebuilt", "bigquery", "--stdio"],
    "env": {"BIGQUERY_PROJECT": os.getenv("GCP_PROJECT_ID")},
}
```

This uses your existing `gcloud` Application Default Credentials, so no extra auth
setup is required. (This is also the configuration used for the Claude Desktop
path in [Module 3](../module-3/README.md#alternative-claude-desktop).)

---

## 4. OpenAI API Errors

### `openai.AuthenticationError: Incorrect API key`

**Cause:** `OPENAI_API_KEY` in `.env` is wrong or expired.

**Fix:**
- Verify the key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
- Make sure there are no trailing spaces in the `.env` value
- The key must start with `sk-`

### `openai.RateLimitError: 429 Too Many Requests`

**Cause:** Hitting OpenAI rate limits (common with large embedding batches).

**Fix:**
- The embedding workflow processes in batches of 100 nodes with automatic retry
- If persistent: wait 60 seconds and re-run the embeddings cell in the notebook

### `openai.BadRequestError: 400 invalid dimensions`

**Cause:** Trying to use a different embedding model than what created the vector index.

**Fix:**
- The workshop uses `text-embedding-3-small` with 768 dimensions
- If you change the model or dimensions, you must delete the vector indexes and regenerate:
  ```cypher
  DROP INDEX column_vector_index IF EXISTS;
  DROP INDEX table_vector_index IF EXISTS;
  DROP INDEX schema_vector_index IF EXISTS;
  DROP INDEX business_term_vector_index IF EXISTS;
  ```
  Then re-run the embeddings cell in `2a_build_metadata_graph.ipynb`.

---

## 5. Embeddings Not Found / Vector Search Returns Nothing

### Vector search returns 0 results

**Cause 1:** Vector index doesn't exist yet.

**Fix:**
```cypher
SHOW VECTOR INDEXES
```

**Cause 2:** Embeddings not generated (all nodes have `embedding: null`).

**Fix:**
```cypher
MATCH (c:Column) WHERE c.embedding IS NULL RETURN count(c)
```
If this returns > 0, embeddings weren't generated. 

**Cause 3:** Similarity threshold too high (default is 0.5).

**Symptom:** The agent uses `get_full_metadata_schema` instead of the semantic tool.
**Note:** This is expected behavior — the agent falls back gracefully. If this happens consistently, check that column descriptions exist in your dataset (descriptions must be populated for embeddings to be meaningful).

---

