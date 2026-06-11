# Module 4: Bring Your Own Data

## What You'll Do

Build a semantic layer from **CSV files describing your own schema** — no BigQuery or cloud data warehouse required. The same ETL pipeline from Module 2 runs against your CSV files and loads the result into Neo4j. The only requirement is that you have an MCP server ready that can connect your semantic layer backed agent to the database(s) of interest.

## How It Works

You describe your schema in CSV files placed in `datasets/user/csv/`. The CSV connector reads those files and creates the same graph structure as the BigQuery connector: `Database → Schema → Table → Column` with FK relationships and optional business term enrichment.

Once loaded, connect the same MCP server to query your data.

## Setup

**Ensure you are using a fresh Neo4j database before running the module 4 notebook!**

1. Add your schema CSV files to `datasets/user/csv/`
2. Run the notebook: [`4_bring_your_own_data.ipynb`](4_bring_your_own_data.ipynb)
3. Connect an agent using the MCP config in [Module 3](../module-3/README.md)

## CSV File Reference

See the CSV Format section in the notebook for full details. The minimum required files are:

| File | What It Describes |
|------|------------------|
| `database_info.csv` | Your database name and description |
| `schema_info.csv` | Schema(s) within the database |
| `table_info.csv` | Tables — **include descriptions for better search accuracy** |
| `column_info.csv` | Columns — **descriptions are the most important input** |
| `column_references_info.csv` | Foreign key relationships between columns |

Optional enrichment files:

| File | What It Describes |
|------|------------------|
| `value_info.csv` | Enumerated values for categorical columns (e.g. `active`, `cancelled`, `pending`) |
| `glossary_info.csv` | A named business glossary |
| `category_info.csv` | Groupings within the glossary |
| `business_term_info.csv` | Individual business terms with definitions |
| `column_term_info.csv` | Links columns to business terms (`TAGGED_WITH`) |


## Tips

- **Column descriptions drive search quality.** A column with no description will not surface in semantic search. The more specific your descriptions, the more accurately the agent picks the right columns.
- **Foreign keys enable multi-table joins.** Without `column_references_info.csv` the agent can find relevant columns but may struggle to write correct JOINs.
- The CSV connector uses `MERGE` — re-running after edits is safe and will not create duplicates.
