"""Entry point for running the Text2SQL agent with MCP server."""

import asyncio
import os

import httpx
from dotenv import load_dotenv
from google.auth import default
from google.auth.transport.requests import Request
from langchain_mcp_adapters.client import MultiServerMCPClient

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

load_dotenv()

SYSTEM_PROMPT = """You are a Text2SQL agent that answers questions about our BigQuery dataset.

You have two kinds of tools:
1. Metadata-graph tools (e.g. `get_context_by_table_hybrid_search`) — these search a
   semantic layer that maps business terms to the real tables, columns, and join keys.
2. `execute_sql` — runs SQL against BigQuery.

MANDATORY WORKFLOW — follow on every single question, no exceptions:
1. FIRST, call a metadata-graph context tool (such as `get_context_by_table_hybrid_search`)
   using the key business terms from the user's question (e.g. "product line", "ARR",
   "revenue"). Never skip this step.
2. Read the returned context to identify the correct tables, exact column names, join keys,
   and any business-term definitions. A business label like "product line" usually maps to a
   descriptive column on a dimension table (e.g. products.name), NOT to a free-text label
   column on a fact/transaction table.
3. ONLY THEN write and run SQL with `execute_sql`.

Hard rules:
* NEVER call `execute_sql` before you have retrieved schema/context from the metadata graph.
* Always qualify tables with project and dataset names (e.g. `acme_corp.subscriptions`).
* When a question asks which entity "contributes the most", aggregate (GROUP BY + SUM) over
  the correct dimension and join to the dimension table to resolve human-readable names —
  do not just return the single largest row.
* NEVER present a raw identifier (e.g. a product_id like 'PROD002', a customer_id, etc.) as
  the answer. Always JOIN to the entity's dimension table and SELECT its descriptive name
  column (e.g. products.name) so the answer names the entity, not its ID.
* Return results to the user in a readable, well-labeled format.

If a metadata tool returns nothing useful, say so explicitly before falling back to SQL."""

DEFAULT_AGENT_MODEL = "gpt-4o-mini"


def create_text2sql_agent(mcp_tools: list[BaseTool]) -> CompiledStateGraph:
    """Create a Text2SQL LangGraph agent with the provided MCP tools."""
    model = ChatLiteLLM(model=os.getenv("AGENT_MODEL", DEFAULT_AGENT_MODEL))
    return create_agent(
        model=model,
        tools=mcp_tools,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
    )

# Custom auth class for Google Cloud
class GoogleAuth(httpx.Auth):
    """Custom httpx auth handler that injects Google Cloud bearer tokens."""

    def __init__(self) -> None:
        """Initialize credentials using the application default credentials."""
        self.credentials, _ = default()

    def auth_flow(self, request):  # noqa: ANN001, ANN201
        """Refresh the token and inject it into the request."""
        self.credentials.refresh(Request())
        request.headers["Authorization"] = f"Bearer {self.credentials.token}"
        yield request


# Env vars forwarded to the MCP subprocess. `StdioServerParameters` rejects
# None values, so any var not set in the parent environment is dropped below.
# Provider auth vars (OPENAI_API_KEY, GEMINI_API_KEY, COHERE_API_KEY, ...) are
# passed through if present so LiteLLM in the MCP server can pick them up.
_mcp_env_candidates = {
    "NEO4J_URI": os.getenv("NEO4J_URI"),
    "NEO4J_USERNAME": os.getenv("NEO4J_USERNAME"),
    "NEO4J_PASSWORD": os.getenv("NEO4J_PASSWORD"),
    "NEO4J_DATABASE": os.getenv("NEO4J_DATABASE"),
    "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
    # Must match what was used to build the graph in Module 2 (defaults to 768).
    "EMBEDDING_DIMENSIONS": os.getenv("EMBEDDING_DIMENSIONS", "768"),
    # This version supports OpenAI embeddings only, so OPENAI_API_KEY is required.
    "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
    # The below providers are only supported in the Neocarta MCP server v0.6.0+
    # "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),  # noqa: ERA001
    # "COHERE_API_KEY": os.getenv("COHERE_API_KEY"),  # noqa: ERA001
    # "AZURE_API_KEY": os.getenv("AZURE_API_KEY"),  # noqa: ERA001
    # "AZURE_API_BASE": os.getenv("AZURE_API_BASE"),  # noqa: ERA001
    # "AZURE_API_VERSION": os.getenv("AZURE_API_VERSION"),  # noqa: ERA001
    # "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID"),  # noqa: ERA001
    # "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY"),  # noqa: ERA001
    # "AWS_REGION_NAME": os.getenv("AWS_REGION_NAME"),  # noqa: ERA001
}
sql_metadata_graph_mcp_params = {
    "transport": "stdio",
    "command": "uvx",
    "args": ["--from", "neocarta[mcp]@0.4.0", "neocarta-mcp"],
    "env": {k: v for k, v in _mcp_env_candidates.items() if v is not None},
}

bigquery_mcp_params = {
    "transport": "http",
    "url": "https://bigquery.googleapis.com/mcp",
    "auth": GoogleAuth(),
    "headers": {
        "Content-Type": "application/json",
    },
}

client = MultiServerMCPClient(
    {
        "sql_metadata_graph": sql_metadata_graph_mcp_params,
        "bigquery": bigquery_mcp_params,
    }
)

CONFIG = {"configurable": {"thread_id": "1"}}


# run the agent with MCP server using stdio transport
async def main() -> None:
    """Connect to MCP servers, build the agent, and run an interactive chat loop."""
    # Get tools per server. The neocarta server self-filters its tool set based on
    # the target database's index inventory, so we trust everything it exposes.
    # The BigQuery MCP server exposes more than we want, so we explicitly allowlist
    # only the SQL execution tool.
    neocarta_tools = await client.get_tools(server_name="sql_metadata_graph")
    bigquery_tools = await client.get_tools(server_name="bigquery")
    bigquery_allowed = {"execute_sql"}
    allowed_tools = list(neocarta_tools) + [
        tool for tool in bigquery_tools if tool.name in bigquery_allowed
    ]

    agent = create_text2sql_agent(allowed_tools)

    # conversation loop
    print("\n===================================== Chat =====================================")
    print("Ask a question about the dataset, or type 'exit' to quit.\n")

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()  # newline so the shell prompt starts cleanly
            break
        if user_input.lower() in {"exit", "quit", "q"}:
            break
        if not user_input.strip():
            continue

        async for chunk in agent.astream(
            {"messages": [{"role": "user", "content": user_input}]},
            stream_mode="values",
            config=CONFIG,
        ):
            # Each chunk is the full state. The first emission echoes our own
            # human message, and tool results arrive as ToolMessages, so we
            # filter on message type to only surface the agent's own output.
            latest_message = chunk["messages"][-1]
            if latest_message.type != "ai":
                continue

            tool_calls = getattr(latest_message, "tool_calls", None)
            if tool_calls:
                names = ", ".join(tc["name"] for tc in tool_calls)
                print(f"Calling tools: {names}")
            if latest_message.content:
                print(f"Agent: {latest_message.content}")


if __name__ == "__main__":
    asyncio.run(main())
