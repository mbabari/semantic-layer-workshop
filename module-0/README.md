# Setup

**Complete this before starting Module 1.**

This workshop requires access to GCP (BigQuery), Neo4j, and OpenAI. All services have free tiers sufficient for the workshop.

---

## 1. GCP Requirements

### Google Cloud CLI (`gcloud`)

The workshop uses `gcloud` to authenticate with BigQuery. Install it before proceeding.

[Google Cloud CLI Install Documentation](https://cloud.google.com/sdk/docs/install)

Verify installation:
```bash
gcloud --version
```

---

### Project & BigQuery

- A GCP project with billing enabled (BigQuery free tier is sufficient for the sample dataset)
- The BigQuery API enabled in your project:
  ```bash
  gcloud services enable bigquery.googleapis.com --project=YOUR_PROJECT_ID
  ```
- Project Owner or Editor role (or at minimum: BigQuery Data Editor + BigQuery Job User)

### Application Default Credentials (ADC)

The workshop uses ADC for BigQuery access. Run:

```bash
gcloud auth application-default login
```

This opens a browser window — authenticate with the account that has BigQuery access.

Verify it worked:
```bash
gcloud auth application-default print-access-token
```
You should see a token string (not an error).

### BigQuery MCP Server

The agent uses the official BigQuery remote MCP server. Enable it for your project:

```bash
gcloud beta services mcp enable bigquery.googleapis.com --project=YOUR_PROJECT_ID
```

Test that it's reachable (replace `YOUR_PROJECT_ID`):
```bash
curl -k \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
      "name": "execute_sql",
      "arguments": {
        "projectId": "YOUR_PROJECT_ID",
        "query": "SELECT 1 AS test"
      }
    }
  }' \
  https://bigquery.googleapis.com/mcp
```

Expected response: a JSON result containing `{"test": 1}`.

---

## 2. Neo4j

### Option A: AuraDB Free Tier (Recommended)

1. Sign up at [neo4j.com/product/auradb](https://neo4j.com/product/auradb/)
2. Create a **Free** instance (no credit card required)
3. Note the connection URI (format: `neo4j+s://xxxxxxxx.databases.neo4j.io`), username (`neo4j`), and the password shown at creation time
4. **Save the password immediately** — AuraDB only shows it once

### Option B: Neo4j Desktop (Local)

1. Download from [neo4j.com/download](https://neo4j.com/download/)
2. Create a new project and local DBMS
3. Start the DBMS; note URI (`bolt://localhost:7687`), username (`neo4j`), password

### Option C: Docker

If you have Docker installed, run a single command:

```bash
docker run \
  --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password \
  neo4j:latest
```

Connection details: URI `bolt://localhost:7687`, username `neo4j`, password `password`.

Open [localhost:7474](http://localhost:7474) in a browser to verify Neo4j is running.

**Required version:** Neo4j 5.x or higher (for vector index support). AuraDB free tier uses the latest Neo4j version by default.

---

## 3. OpenAI API Key

- Create or log in to an account at [platform.openai.com](https://platform.openai.com)
- Generate an API key: Settings → API Keys → Create new secret key
- Ensure the key has access to `text-embedding-3-small`

OpenAI is used **only for generating embeddings** (Module 2 and the MCP server). Module 3 may be completed with any agentic application.

---

## 4. Python Environment

- **Python 3.12 or higher** — check with `python3 --version`
- **uv** package manager — install with:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
  Verify: `uv --version`

---

## 5. Environment Setup

Clone the repo and install dependencies:

```bash
git clone https://github.com/neo4j-field/semantic-layer-workshop.git
cd semantic-layer-workshop
uv sync
```

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

Open `.env` and set each variable:

```bash
# GCP project for BigQuery
GCP_PROJECT_ID=your-gcp-project-id

# BigQuery dataset ID (pre-loaded ACME Corp sample dataset)
BIGQUERY_DATASET_ID=acme_corp

# Neo4j connection — AuraDB: neo4j+s://xxxxxxxx.databases.neo4j.io
#                   Desktop/Docker: bolt://localhost:7687
NEO4J_URI=neo4j+s://YOUR_AURA_INSTANCE.databases.neo4j.io
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=YOUR_PASSWORD
NEO4J_DATABASE=neo4j

# OpenAI API key (used for embeddings)
OPENAI_API_KEY=sk-...your-key-here...
```

---

## 6. Pre-Workshop Smoke Test

Run this checklist before starting Module 1:

- [ ] `gcloud --version` prints a version string
- [ ] `gcloud auth application-default print-access-token` returns a token
- [ ] BigQuery MCP curl test (above) returns a valid JSON response
- [ ] Neo4j Browser opens and you can log in at your AuraDB/Desktop/Docker URI
- [ ] `python3 --version` shows 3.12 or higher
- [ ] `uv --version` prints a version string

If any step fails, see [`../reference/troubleshooting.md`](../reference/troubleshooting.md).

---

→ [Module 1: Introduction](../module-1/README.md)
