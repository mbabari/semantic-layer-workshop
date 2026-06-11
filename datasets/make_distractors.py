"""Generate described 'distractor' tables to inflate a BigQuery dataset for the
neocarta scaling test.

These live in domains deliberately distinct from the acme SaaS-revenue core
(manufacturing, IoT, logistics, network telemetry, security, ML-ops, ...), each
with a table description and per-column descriptions, so they are realistic
haystack tables that semantic search must look past to find the acme needles.

Prints a BigQuery DDL script to stdout:
    uv run datasets/make_distractors.py > /tmp/distractors.sql
"""

import os

PROJECT = os.getenv("GCP_PROJECT_ID")
if not PROJECT:
    raise SystemExit("Set GCP_PROJECT_ID in .env before running this script.")
DATASET = os.getenv("DW_DATASET", "acme_dw")

# domain -> (table-noun, [ (col, type, desc-template) ... ]) used to synthesize variants
DOMAINS = {
    "mfg": (
        "Manufacturing / shop-floor operations.",
        ["work_order", "machine", "production_run", "defect", "bom", "routing",
         "downtime_event", "quality_check", "shift", "tooling", "maintenance_log",
         "scrap_record", "assembly_station", "calibration", "yield_metric"],
    ),
    "iot": (
        "IoT device telemetry and edge fleet management.",
        ["sensor_reading", "device", "gateway", "firmware_version", "alert",
         "battery_status", "telemetry_stream", "edge_node", "signal_quality",
         "provisioning_event", "heartbeat", "anomaly_score"],
    ),
    "logistics": (
        "Supply chain, warehousing and freight logistics.",
        ["shipment", "carrier", "warehouse_bin", "pick_task", "delivery_route",
         "freight_invoice", "customs_declaration", "pallet", "dock_appointment",
         "inventory_movement", "lane_rate", "container"],
    ),
    "network": (
        "Network and infrastructure observability.",
        ["flow_record", "interface", "bgp_session", "latency_probe", "packet_drop",
         "circuit", "dns_query_log", "firewall_rule_hit", "bandwidth_sample",
         "topology_link", "outage"],
    ),
    "security": (
        "Security, audit and access governance.",
        ["audit_event", "access_grant", "secret_rotation", "vuln_finding",
         "login_attempt", "policy_violation", "cert_inventory", "threat_indicator",
         "privilege_escalation", "data_classification"],
    ),
    "mlops": (
        "ML platform: features, models and inference.",
        ["feature_value", "model_version", "training_run", "inference_log",
         "drift_metric", "label_set", "experiment", "embedding_index",
         "pipeline_run", "dataset_snapshot"],
    ),
    "fin": (
        "Corporate finance ledger and treasury (general accounting, not SaaS revenue).",
        ["gl_entry", "journal", "cost_center_actuals", "fx_rate", "tax_lot",
         "bank_reconciliation", "fixed_asset", "depreciation_schedule",
         "intercompany_transfer", "budget_line"],
    ),
    "facilities": (
        "Facilities, real estate and energy management.",
        ["meter_reading", "space_booking", "hvac_zone", "badge_swipe",
         "work_request", "lease_amortization", "energy_invoice", "occupancy_sample"],
    ),
    "field": (
        "Field service dispatch and asset servicing (industrial equipment).",
        ["service_visit", "technician_route", "part_consumption", "warranty_claim",
         "asset_meter", "dispatch", "sla_breach_industrial"],
    ),
    "telecom": (
        "Telecom billing and usage rating (CDR-level).",
        ["call_detail_record", "rating_event", "cell_site", "subscriber_line",
         "roaming_session", "usage_bucket", "interconnect_charge"],
    ),
}

# generic column templates appended to each table for realistic width
COMMON = [
    ("id", "STRING", "Surrogate primary key for the {noun} record."),
    ("created_at", "TIMESTAMP", "UTC timestamp when this {noun} row was created."),
    ("updated_at", "TIMESTAMP", "UTC timestamp of the last update to this {noun} row."),
    ("status", "STRING", "Lifecycle status of the {noun} (e.g. open, active, closed)."),
    ("region", "STRING", "Geographic region associated with the {noun}."),
    ("owner_id", "STRING", "Identifier of the employee or system owning the {noun}."),
    ("amount", "NUMERIC", "Primary numeric measure for the {noun} (units depend on domain)."),
    ("notes", "STRING", "Free-text operational notes about the {noun}."),
]


def col_ddl(name: str, typ: str, desc: str) -> str:
    safe = desc.replace('"', "'")
    return f'  {name} {typ} OPTIONS(description="{safe}")'


def main() -> None:
    print(f"-- distractor tables for {PROJECT}.{DATASET}")
    n = 0
    for prefix, (domain_desc, nouns) in DOMAINS.items():
        for noun in nouns:
            table = f"{prefix}_{noun}s".replace("__", "_")
            cols = [col_ddl(f"{noun}_id", "STRING", f"Natural key of the {noun}.")]
            # a couple of domain-flavoured columns
            cols.append(col_ddl(f"{noun}_type", "STRING",
                                 f"Category/type of the {noun} within {prefix} operations."))
            cols.append(col_ddl(f"{noun}_value", "FLOAT64",
                                 f"Measured value or metric for the {noun}."))
            for cname, ctype, ctmpl in COMMON:
                cols.append(col_ddl(f"{cname}", ctype, ctmpl.format(noun=noun)))
            tdesc = f"{domain_desc} One row per {noun}.".replace('"', "'")
            print(f'CREATE TABLE IF NOT EXISTS `{PROJECT}.{DATASET}.{table}` (')
            print(",\n".join(cols))
            print(f') OPTIONS(description="{tdesc}");')
            n += 1
    print(f"-- generated {n} distractor tables", flush=True)


if __name__ == "__main__":
    main()
