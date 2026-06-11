"""Interactive Text2SQL agent for the Census ACS demo -- WITH vs WITHOUT Neocarta.

This is a self-contained demo agent for the `census_bureau_acs` use case. It does
NOT touch the workshop's other programs (3_agent.py, measure_tokens.py, the
benchmarks). Use it to show a customer, live, how the Neocarta semantic layer
changes an agent's behaviour on a huge, confusing schema (278 near-identical
tables, ~252 columns each, ~475K-token full schema that does not fit in context).

Run the SAME question in each mode and watch the difference:

    # No semantic layer: the agent must brute-force the 278-table schema.
    ./.venv/bin/python module-3/census_agent.py --mode without

    # With Neocarta: one targeted retrieval finds the single right table.
    ./.venv/bin/python module-3/census_agent.py --mode with

Options:
    --mode with|without   (default: with)
    --model <name>        (default: $AGENT_MODEL or gpt-4o-mini; try gpt-4o)

After every question it prints a tally (input/output tokens, est. cost, #tool
calls, tables queried) AND a per-message token breakdown showing exactly where the
tokens go (each tool result's size, and how big results get re-sent as model input
on every later call). Pass --no-trace to hide the breakdown.

Connection summary:
  * Metadata / semantic layer -> Neocarta MCP (stdio via uvx) against the `census`
    Neo4j database built by module-2/build_census_semantic_layer.py.
  * Query execution           -> a local, cost-capped BigQuery executor that runs
    against the public dataset bigquery-public-data.census_bureau_acs in place.
"""

import argparse
import asyncio
import os
import re
from pathlib import Path

import litellm
import tiktoken
from dotenv import load_dotenv
from google.cloud import bigquery

# Silence LiteLLM's "Give Feedback / Get Help" banner + info lines that it prints to
# stdout on provider errors (e.g. an OpenAI 429 TPM rate limit). We surface our own
# concise message instead, so the demo / sweep output stays clean.
litellm.suppress_debug_info = True
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient

from langchain.agents import create_agent
from langchain.tools import BaseTool
from langchain_litellm import ChatLiteLLM
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

try:
    from litellm.exceptions import RateLimitError
except Exception:  # noqa: BLE001 — fall back if litellm internals move
    RateLimitError = ()  # type: ignore[assignment]

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=True)

# Keep the demo terminal clean. The neocarta MCP subprocess and the Neo4j driver
# write verbose notifications/warnings to stderr (e.g. the vector-search Cypher and
# GqlStatusObject position info) that look alarming to a customer but are NOT
# failures. langchain_mcp_adapters gives no hook to redirect the child's stderr, so
# we redirect this process's stderr (fd 2 -> a log file), which the child inherits.
# All demo output uses print() -> stdout and is unaffected. Set CENSUS_QUIET=0 to keep
# stderr on screen for debugging.
STDERR_LOG: str | None = None
if os.getenv("CENSUS_QUIET", "1").lower() not in {"0", "false", "no"}:
    STDERR_LOG = os.getenv("CENSUS_STDERR_LOG", "/tmp/census_agent.stderr.log")
    _errlog_fh = open(STDERR_LOG, "w", buffering=1)  # noqa: SIM115
    os.dup2(_errlog_fh.fileno(), 2)

SOURCE_PROJECT = os.getenv("CENSUS_SOURCE_PROJECT", "bigquery-public-data")
SOURCE_DATASET = os.getenv("CENSUS_SOURCE_DATASET", "census_bureau_acs")
NEO4J_DATABASE = os.getenv("CENSUS_NEO4J_DATABASE", "census")
BILLING_PROJECT = os.getenv("GCP_PROJECT_ID")
TABLE_FQN = f"{SOURCE_PROJECT}.{SOURCE_DATASET}"

MAX_BYTES_BILLED = int(os.getenv("MAX_BYTES_BILLED", str(15 * 10**9)))  # cost safety cap
RECURSION_LIMIT = int(os.getenv("RECURSION_LIMIT", "40"))
# LiteLLM retries on transient errors (e.g. rate limits). Lower it for a fast sweep
# where a doomed combo (gpt-4o brute-force vs a 30K TPM cap) shouldn't grind through
# 8 backoffs: e.g. CENSUS_NUM_RETRIES=1 ./.venv/bin/python ... --runall
NUM_RETRIES = int(os.getenv("CENSUS_NUM_RETRIES", "8"))

# Targeted retrieval only -- never expose get_full_metadata_schema (full-catalog
# dump) or the list_* tools, the same way the workshop allowlists BigQuery to execute_sql.
NEOCARTA_ALLOWED = {
    "get_context_by_table_hybrid_search",
    "get_context_by_column_hybrid_search",
    "get_context_by_schema_and_table_vector_search",
}

# LLMs offerable in the startup menu. Each entry is (litellm_model_id, label,
# required_api_key_env). Models whose key is missing from .env are hidden. The
# agent runs through ChatLiteLLM, which reads OPENAI_API_KEY / ANTHROPIC_API_KEY
# from the environment automatically.
# Anthropic ids carry the litellm "anthropic/" provider prefix (required, or
# litellm raises "LLM Provider NOT provided"). OpenAI ids are recognised as-is.
MODEL_MENU = [
    ("gpt-4o-mini", "OpenAI  gpt-4o-mini", "OPENAI_API_KEY"),
    ("gpt-4o", "OpenAI  gpt-4o         - strong, pricier", "OPENAI_API_KEY"),
    ("anthropic/claude-haiku-4-5-20251001", "Anthropic  Claude Haiku 4.5  - cheap & fast", "ANTHROPIC_API_KEY"),
    ("anthropic/claude-sonnet-4-5-20250929", "Anthropic  Claude Sonnet 4.5 - strong, balanced", "ANTHROPIC_API_KEY"),
    ("anthropic/claude-opus-4-5-20251101", "Anthropic  Claude Opus 4.5   - most capable, pricier", "ANTHROPIC_API_KEY"),
]

# Business-vocabulary questions: the wording (voting district, rent burden) and/or
# the year/span force the agent to find the right table out of 278 near-identical
# ones. A naive agent can't reliably guess the cryptic name and must scan; Neocarta
# maps the business term to the right table via its description. All four were
# verified to retrieve the correct table WITH Neocarta end-to-end.
# NOTE: avoid questions about overlapping "area" geographies (metropolitan area /
# CBSA vs PUMA vs place vs block group) -- those concepts are too semantically
# similar for table-level vector search to separate reliably.
DEMO_QUESTIONS = [
    "What is the rent burden (share of household income spent on rent) by county in "
    "the 2017 5-year ACS?",
    "Rank congressional voting districts by the number of residents with a "
    "bachelor's degree, using the 2016 1-year ACS.",
    "Rank U.S. states by median age (oldest first) using the 2015 5-year ACS.",
    "What are the 10 counties with the highest median household income in the "
    "2018 5-year ACS?",
]

_bq = bigquery.Client(project=BILLING_PROJECT)
_TABLES_CACHE: list[str] = []


@tool
def execute_sql(sql: str) -> str:
    """Execute a BigQuery Standard SQL query and return up to 50 rows.

    Tables must be fully qualified, e.g. `bigquery-public-data.census_bureau_acs.county_2018_5yr`.
    Select only the columns you need.
    """
    try:
        job = _bq.query(sql, job_config=bigquery.QueryJobConfig(maximum_bytes_billed=MAX_BYTES_BILLED))
        rows = [dict(r) for r in job.result(max_results=50)]
        return f"OK. Rows: {rows}"
    except Exception as e:
        return f"ERROR: {e}"


@tool
def list_census_tables() -> str:
    """List all table names in the Census ACS dataset."""
    global _TABLES_CACHE
    if not _TABLES_CACHE:
        rows = _bq.query(
            f"SELECT table_name FROM `{TABLE_FQN}`.INFORMATION_SCHEMA.TABLES ORDER BY table_name"
        ).result()
        _TABLES_CACHE = [r["table_name"] for r in rows]
    return "Tables: " + ", ".join(_TABLES_CACHE)


@tool
def get_table_columns(table_name: str) -> str:
    """Return the column names and data types for one Census ACS table."""
    rows = _bq.query(
        f"SELECT column_name, data_type FROM `{TABLE_FQN}`.INFORMATION_SCHEMA.COLUMNS "
        f"WHERE table_name = @t ORDER BY ordinal_position",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("t", "STRING", table_name)]
        ),
    ).result()
    cols = [f"{r['column_name']} {r['data_type']}" for r in rows]
    if not cols:
        return f"No such table: {table_name}"
    return f"{table_name} ({len(cols)} columns): " + ", ".join(cols)


PROMPT_WITH = f"""You are a Text2SQL agent for the U.S. Census ACS dataset in BigQuery.
Tables live in `{TABLE_FQN}` and must be fully qualified as `{TABLE_FQN}.<table>`.

This dataset has hundreds of near-identical tables that differ only by geography
level, year, and survey span (e.g. county_2018_5yr vs cbsa_2017_1yr), each with
~252 columns.

MANDATORY before writing SQL:
* Call get_context_by_table_hybrid_search ONCE with the key terms from the
  question (geography + year + survey span + the measure) and set max_tables=1 so
  you retrieve only the single best-matching table and its columns.
* Do not call it repeatedly. One targeted retrieval is enough.
Then call execute_sql.

Rules:
* Never SELECT * (tables have ~252 columns). Select only what you need.
* Pick the exact geography/year/span the question asks for.
* Give a short, readable final answer."""

PROMPT_WITHOUT = f"""You are a Text2SQL agent for the U.S. Census ACS dataset in BigQuery.
Tables must be fully qualified as `{TABLE_FQN}.<table>`.

This dataset has hundreds of near-identical tables that differ only by geography
level, year, and survey span. The full schema is far too large to show at once.
Use list_census_tables to see table names and get_table_columns to inspect a
table's columns. Discover the correct table and columns, then call execute_sql.

Rules:
* Never SELECT * (tables have ~252 columns). Select only what you need.
* Pick the exact geography/year/span the question asks for.
* Give a short, readable final answer."""


def estimate_cost(model: str, tin: int, tout: int) -> float | None:
    try:
        from litellm import cost_per_token

        pin, pout = cost_per_token(model=model, prompt_tokens=tin, completion_tokens=tout)
        return pin + pout
    except Exception:
        return None


def tally(messages: list):
    tin = tout = 0
    calls: list[str] = []
    tables: set[str] = set()
    for m in messages:
        um = getattr(m, "usage_metadata", None)
        if um:
            tin += um.get("input_tokens", 0)
            tout += um.get("output_tokens", 0)
        if isinstance(m, AIMessage):
            for tc in m.tool_calls or []:
                calls.append(tc["name"])
                if tc["name"] == "execute_sql":
                    tables.update(re.findall(rf"{re.escape(SOURCE_DATASET)}\.([a-z0-9_]+)", tc["args"].get("sql", "")))
    return tin, tout, calls, tables


_ENC = tiktoken.get_encoding("o200k_base")


def _toklen(x) -> int:
    return len(_ENC.encode(x if isinstance(x, str) else str(x)))


def print_trace(messages: list) -> None:
    """Show where the tokens go: size of each message, and how the big tool results
    get re-sent as model input on every subsequent call (stateless replay)."""
    print("\n  TOKEN TRACE — where the tokens go (content size via o200k_base):")
    print(f"  {'#':>2}  {'role':<10}{'content':>9}  detail")
    tool_total = 0
    for i, m in enumerate(messages, 1):
        content = m.content if isinstance(m.content, str) else str(m.content)
        ctok = _toklen(content)
        role, detail = m.type, ""
        if isinstance(m, AIMessage):
            role = "ai"
            tcs = m.tool_calls or []
            if tcs:
                detail = "-> " + ", ".join(tc["name"] for tc in tcs)
            um = getattr(m, "usage_metadata", None)
            if um:
                detail += f"   [model in={um.get('input_tokens', 0):,} out={um.get('output_tokens', 0):,}]"
        elif m.type == "tool":
            detail = f"{getattr(m, 'name', 'tool')} RESULT"
            tool_total += ctok
        elif m.type == "system":
            detail = "system prompt"
        elif m.type == "human":
            detail = "question"
        flag = "  <== bulk" if (m.type == "tool" and ctok > 3000) else ""
        print(f"  {i:>2}  {role:<10}{ctok:>9,}  {detail}{flag}")
    ai_msgs = [m for m in messages if isinstance(m, AIMessage) and getattr(m, "usage_metadata", None)]
    ins = sum(m.usage_metadata.get("input_tokens", 0) for m in ai_msgs)
    outs = sum(m.usage_metadata.get("output_tokens", 0) for m in ai_msgs)
    print(f"\n  tool results returned once : {tool_total:,} tok")
    print(f"  billed model INPUT total   : {ins:,} tok  across {len(ai_msgs)} model call(s)")
    print(f"  billed model OUTPUT total  : {outs:,} tok")
    print("  -> a big tool result (e.g. neocarta context) is re-sent as input on every later")
    print("     model call, so input ~= (context size) x (number of model calls).\n")


def choose_model(default: str) -> str:
    """Interactive menu of available LLMs (only those whose API key is set)."""
    available = [(mid, label) for mid, label, keyvar in MODEL_MENU if os.getenv(keyvar)]
    if not available:
        return default
    print("\nChoose an LLM for the agent:")
    for i, (mid, label) in enumerate(available, 1):
        print(f"  {i}. {label}")
    default_idx = next((i for i, (mid, _) in enumerate(available, 1) if mid == default), 1)
    while True:
        sel = input(f"Model [{default_idx}]: ").strip() or str(default_idx)
        if sel.isdigit() and 1 <= int(sel) <= len(available):
            return available[int(sel) - 1][0]
        print("  invalid choice, try again.")


def make_mcp_client() -> MultiServerMCPClient:
    """Neocarta MCP (stdio) against the census Neo4j database."""
    return MultiServerMCPClient(
        {
            "neocarta": {
                "transport": "stdio",
                "command": "uvx",
                "args": ["--from", "neocarta[mcp]@0.4.0", "neocarta-mcp"],
                "env": {
                    k: v
                    for k, v in {
                        "NEO4J_URI": os.getenv("NEO4J_URI"),
                        "NEO4J_USERNAME": os.getenv("NEO4J_USERNAME"),
                        "NEO4J_PASSWORD": os.getenv("NEO4J_PASSWORD"),
                        "NEO4J_DATABASE": NEO4J_DATABASE,
                        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
                        "EMBEDDING_MODEL": os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                        "EMBEDDING_DIMENSIONS": os.getenv("EMBEDDING_DIMENSIONS", "768"),
                    }.items()
                    if v is not None
                },
            }
        }
    )


def build_tools_prompt(mode: str, neocarta_tools: list) -> tuple[list[BaseTool], str]:
    """Return (tools, system_prompt) for the requested mode. WITH exposes only the
    targeted neocarta retrieval tools (no full-schema-dump); WITHOUT uses brute-force
    BigQuery discovery."""
    if mode == "with":
        tools: list[BaseTool] = [t for t in neocarta_tools if t.name in NEOCARTA_ALLOWED] + [execute_sql]
        prompt = PROMPT_WITH
    else:
        tools = [list_census_tables, get_table_columns, execute_sql]
        prompt = PROMPT_WITHOUT
    for t in tools:
        t.handle_tool_error = True
    return tools, prompt


def make_agent(model: str, tools: list[BaseTool], prompt: str):
    return create_agent(
        model=ChatLiteLLM(model=model, num_retries=NUM_RETRIES, temperature=0),
        tools=tools,
        system_prompt=prompt,
        checkpointer=InMemorySaver(),
    )


def short_model(model_id: str) -> str:
    """Compact display label: 'anthropic/claude-opus-4-5-20251101' -> 'claude-opus-4-5'."""
    return re.sub(r"-\d{8}$", "", model_id.split("/")[-1])


async def run_silent(agent, question: str, config: dict) -> tuple[list, bool]:
    """Run one question to completion without streaming. Returns (messages, finished).
    Any limit/error is swallowed so a sweep never aborts; partial messages are recovered."""
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": question}]}, config=config
        )
        return result["messages"], True
    except Exception:  # noqa: BLE001 — GraphRecursionError, RateLimitError, provider errors
        try:
            state = await agent.aget_state(config)
            return state.values.get("messages", []), False
        except Exception:  # noqa: BLE001
            return [], False


def print_runall_table(rows: list[dict]) -> None:
    """Per-question results plus a WITH-vs-WITHOUT summary per model."""
    print("\n" + "=" * 104)
    print("  PER-QUESTION RESULTS")
    print("=" * 104)
    hdr = (f"  {'MODEL':<18}{'MODE':<9}{'Q':<3}{'IN':>9}{'OUT':>7}{'TOTAL':>9}"
           f"{'COST':>10}{'CALLS':>6}{'OK':>4}  TABLE")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        tbl = (",".join(r["tables"]) or "-")[:30]
        print(f"  {r['model']:<18}{r['mode']:<9}{r['q']:<3}{r['in']:>9,}{r['out']:>7,}"
              f"{r['total']:>9,}{('$' + format(r['cost'], '.4f')):>10}{r['calls']:>6}"
              f"{('Y' if r['ok'] else 'N'):>4}  {tbl}")

    print("\n" + "=" * 104)
    print("  SUMMARY — average per question (compare WITH vs WITHOUT within a model row)")
    print("=" * 104)
    sh = (f"  {'MODEL':<18}{'WITHOUT tok':>13}{'WITH tok':>11}{'SAVING':>9}"
          f"{'WITHOUT $':>12}{'WITH $':>11}{'OK w/o':>9}{'OK with':>9}")
    print(sh)
    print("  " + "-" * (len(sh) - 2))
    models: list[str] = []
    for r in rows:
        if r["model"] not in models:
            models.append(r["model"])

    def avg(lst, key):
        return sum(x[key] for x in lst) / len(lst) if lst else 0

    for m in models:
        wo = [r for r in rows if r["model"] == m and r["mode"] == "without"]
        wi = [r for r in rows if r["model"] == m and r["mode"] == "with"]
        wot, wit = avg(wo, "total"), avg(wi, "total")
        save = 100 * (wot - wit) / wot if wot else 0
        woc, wic = avg(wo, "cost"), avg(wi, "cost")
        okw = f"{sum(1 for x in wo if x['ok'])}/{len(wo)}"
        oki = f"{sum(1 for x in wi if x['ok'])}/{len(wi)}"
        print(f"  {m:<18}{wot:>13,.0f}{wit:>11,.0f}{save:>8.0f}%"
              f"{('$' + format(woc, '.4f')):>12}{('$' + format(wic, '.4f')):>11}{okw:>9}{oki:>9}")

    print("\n  SAVING% = (without - with)/without per question (positive = neocarta cheaper).")
    print("  OK = questions that finished without hitting the step limit / a provider error.")
    print("  Token counts across providers use different tokenizers — compare WITHIN a row,")
    print("  not raw totals across providers.\n")


async def run_all(client: MultiServerMCPClient, args) -> None:
    """Sweep every (model x demo question x mode) and print a comparison table."""
    if args.model:
        models = [args.model]
    else:
        models = [mid for mid, _, keyvar in MODEL_MENU if os.getenv(keyvar)]
    if not models:
        print("No models available (no API keys set). Set OPENAI_API_KEY / ANTHROPIC_API_KEY.")
        return

    neocarta_tools = await client.get_tools(server_name="neocarta")

    print("\n" + "=" * 80)
    print("  RUN-ALL SWEEP: models x questions x {without, with} Neocarta")
    print(f"  MODELS   : {[short_model(m) for m in models]}")
    print(f"  QUESTIONS: {len(DEMO_QUESTIONS)}   GRAPH: `{NEO4J_DATABASE}`   DATA: {TABLE_FQN}")
    runs = len(models) * 2 * len(DEMO_QUESTIONS)
    print(f"  TOTAL RUNS: {runs}")
    if STDERR_LOG:
        print(f"  (server/driver logs -> {STDERR_LOG})")
    print("  Note: gpt-4o WITHOUT may hit the 30K TPM cap on large prompts; such runs are")
    print("        marked not-OK with their partial tally rather than aborting the sweep.")
    print("=" * 80)

    rows: list[dict] = []
    for model in models:
        for mode in ("without", "with"):
            tools, prompt = build_tools_prompt(mode, neocarta_tools)
            agent = make_agent(model, tools, prompt)
            for qi, q in enumerate(DEMO_QUESTIONS, 1):
                cfg = {"configurable": {"thread_id": f"{short_model(model)}|{mode}|{qi}"},
                       "recursion_limit": RECURSION_LIMIT}
                print(f"  .. {short_model(model):<18}{mode:<9}Q{qi} ", end="", flush=True)
                msgs, finished = await run_silent(agent, q, cfg)
                tin, tout, calls, tables = tally(msgs)
                cost = estimate_cost(model, tin, tout)
                rows.append({
                    "model": short_model(model), "mode": mode, "q": qi,
                    "in": tin, "out": tout, "total": tin + tout, "cost": cost or 0.0,
                    "calls": len(calls), "tables": sorted(tables), "ok": finished,
                })
                print(f"-> total={tin + tout:>7,}  calls={len(calls)}  "
                      f"{'ok' if finished else 'FAIL'}", flush=True)

    print_runall_table(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["with", "without"], default="with",
                        help="with = use Neocarta semantic layer; without = brute-force discovery")
    parser.add_argument("--model", default=None,
                        help="LiteLLM model id (e.g. gpt-4o, claude-3-5-sonnet-20241022). "
                             "If omitted, you'll get an interactive menu.")
    parser.add_argument("--no-trace", dest="trace", action="store_false",
                        help="Suppress the per-message token breakdown (shown by default).")
    parser.add_argument("--runall", action="store_true",
                        help="Non-interactive sweep: run every model x demo question x "
                             "{without, with} Neocarta and print a comparison table. "
                             "Restrict to one model with --model; otherwise all with API keys set.")
    parser.set_defaults(trace=True)
    args = parser.parse_args()

    client = make_mcp_client()

    # Non-interactive full sweep.
    if args.runall:
        await run_all(client, args)
        return

    # Pick the model: explicit --model wins; otherwise show the interactive menu.
    model = args.model or choose_model(default=os.getenv("AGENT_MODEL", "gpt-4o-mini"))

    neocarta_tools = await client.get_tools(server_name="neocarta") if args.mode == "with" else []
    tools, prompt = build_tools_prompt(args.mode, neocarta_tools)
    agent = make_agent(model, tools, prompt)

    banner = "WITH Neocarta semantic layer" if args.mode == "with" else "WITHOUT Neocarta (brute-force discovery)"
    print("\n" + "=" * 80)
    print(f"  MODE : {banner}")
    print(f"  MODEL: {model}")
    print(f"  GRAPH: Neo4j db `{NEO4J_DATABASE}`  |  DATA: {TABLE_FQN} (278 tables, ~475K-token schema)")
    print(f"  TOOLS: {[t.name for t in tools]}")
    if STDERR_LOG:
        print(f"  (server/driver logs -> {STDERR_LOG}; set CENSUS_QUIET=0 to show on screen)")
    print("=" * 80)
    print("\nDemo questions you can paste:")
    for i, q in enumerate(DEMO_QUESTIONS, 1):
        print(f"  {i}. {q}")
    print("\nType a question (or 1-4 to use a demo question), 'exit' to quit.\n")

    turn = 0
    while True:
        try:
            user_input = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if user_input.lower() in {"exit", "quit", "q"}:
            break
        if not user_input:
            continue
        if user_input in {"1", "2", "3", "4"}:
            user_input = DEMO_QUESTIONS[int(user_input) - 1]
            print(f"> {user_input}")

        turn += 1
        config = {"configurable": {"thread_id": f"t{turn}"}, "recursion_limit": RECURSION_LIMIT}
        finished = True
        last_messages: list = []
        try:
            async for chunk in agent.astream(
                {"messages": [{"role": "user", "content": user_input}]},
                stream_mode="values",
                config=config,
            ):
                last_messages = chunk["messages"]
                latest = last_messages[-1]
                if latest.type != "ai":
                    continue
                if getattr(latest, "tool_calls", None):
                    print("  -> calling tools:", ", ".join(tc["name"] for tc in latest.tool_calls))
                if latest.content:
                    print(f"\nAgent: {latest.content}\n")
        except GraphRecursionError:
            finished = False
            state = await agent.aget_state(config)
            last_messages = state.values.get("messages", [])
            print(f"\n[!] Agent hit the {RECURSION_LIMIT}-step limit and gave up without answering "
                  "(it kept scanning metadata).\n")
        except RateLimitError:
            finished = False
            state = await agent.aget_state(config)
            last_messages = state.values.get("messages", [])
            print(f"\n[!] Hit the model provider's tokens-per-minute (TPM) cap mid-run. The "
                  f"brute-force path's token volume saturated the {model} quota before it "
                  "could finish.\n    Options: use --model gpt-4o-mini (higher TPM), wait ~60s, "
                  "or raise the org's rate limit. (The partial tally below shows how far it got.)\n")
        except Exception as e:  # noqa: BLE001 — never crash a live demo mid-question
            finished = False
            try:
                state = await agent.aget_state(config)
                last_messages = state.values.get("messages", [])
            except Exception:  # noqa: BLE001
                pass
            print(f"\n[!] Run failed: {type(e).__name__}: {str(e)[:160]}\n")

        tin, tout, calls, tables = tally(last_messages)
        cost = estimate_cost(model, tin, tout)
        cost_s = f"${cost:.4f}" if cost is not None else "n/a"
        print("-" * 80)
        print(f"  [{banner} | LLM={model}]  tokens in={tin:,} out={tout:,} "
              f"total={tin + tout:,}  est_cost={cost_s}")
        print(f"  tool_calls={len(calls)}  tables_queried={sorted(tables) or '(none)'}  "
              f"{'finished' if finished else 'DID NOT FINISH'}")
        print("-" * 80 + "\n")

        if args.trace and last_messages:
            print_trace(last_messages)


if __name__ == "__main__":
    asyncio.run(main())
