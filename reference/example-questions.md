# Acme Corp — Exploratory Analytics Questions

A set of sample questions to help new users explore the Acme Corp semantic layer. Each question is answered against the live BigQuery dataset and includes the SQL used to produce the result.

---

## Easy

### 1. Which product line contributes the most to total ARR, and what is the breakdown?

**Tables:** `subscriptions`, `products`

```sql
SELECT
  p.name                                                        AS product,
  COUNT(s.subscription_id)                                      AS active_subscriptions,
  SUM(s.arr_usd)                                                AS arr_usd,
  ROUND(SUM(s.arr_usd) / SUM(SUM(s.arr_usd)) OVER () * 100, 1) AS pct_of_total
FROM acme_corp.subscriptions s
JOIN acme_corp.products p ON s.product_id = p.product_id
WHERE s.status = 'active'
GROUP BY p.name
ORDER BY arr_usd DESC
```

**Expected result:**

| Product | Active subscriptions | ARR (USD) | % of total |
|---|---|---|---|
| Acme Graph DB Enterprise | 3 | $3,180,000 | 82.9% |
| Acme Cloud | 4 | $314,000 | 8.2% |
| Acme GraphRAG | 1 | $240,000 | 6.3% |
| Acme Graph Data Science | 1 | $100,000 | 2.6% |

**Insight:** Graph DB Enterprise is the dominant revenue driver, accounting for over 80% of ARR despite having fewer subscriptions than Cloud. This concentration in a single product line is a key business risk to monitor.

---

### 2. Which subscriptions are renewing in the next 90 days, ranked by ARR at risk?

**Tables:** `subscriptions`

```sql
SELECT
  subscription_id,
  customer_id,
  plan_name,
  arr_usd,
  renewal_date
FROM acme_corp.subscriptions
WHERE status = 'active'
  AND renewal_date BETWEEN CURRENT_DATE() AND DATE_ADD(CURRENT_DATE(), INTERVAL 90 DAY)
ORDER BY arr_usd DESC
```

**Expected result** *(as of Q4 2025 data):*

| Subscription | Customer | Plan | ARR (USD) | Renewal date |
|---|---|---|---|---|
| SUB002 | CUST001 | GDS Enterprise | $100,000 | 2026-01-01 |
| SUB008 | CUST006 | Cloud Pro | $80,000 | 2026-03-01 |
| SUB005 | CUST004 | Cloud Pro | $60,000 | 2026-01-01 |
| SUB010 | CUST008 | Cloud Growth | $54,000 | 2026-01-01 |

**Insight:** $294,000 in ARR is up for renewal in Q1 2026. Three of the four renewals fall on January 1st, making early Q1 a critical period for CSM outreach.

---

### 3. What is the total committed annual vendor spend by category?

**Tables:** `vendor_contracts`, `vendors`

```sql
SELECT
  v.category,
  COUNT(vc.contract_id) AS active_contracts,
  SUM(vc.annual_spend_usd) AS total_annual_spend_usd
FROM acme_corp.vendor_contracts vc
JOIN acme_corp.vendors v ON vc.vendor_id = v.vendor_id
WHERE vc.status = 'active'
GROUP BY v.category
ORDER BY total_annual_spend_usd DESC
```

**Expected result:**

| Category | Active contracts | Annual spend (USD) |
|---|---|---|
| SaaS | 6 | $4,340,000 |
| Consulting | 2 | $510,000 |
| Hardware | 1 | $180,000 |

**Insight:** SaaS tooling dominates Acme's vendor spend at $4.34M annually across 6 contracts — nearly 88% of total committed external spend. Worth reviewing for consolidation opportunities.

---

## Intermediate

### 4. What is the win rate and average deal size by customer segment?

**Tables:** `opportunities`, `customers`

```sql
SELECT
  c.segment,
  COUNT(*)                                                                AS total_opportunities,
  SUM(CASE WHEN o.stage = 'closed_won'  THEN 1 ELSE 0 END)               AS won,
  SUM(CASE WHEN o.stage = 'closed_lost' THEN 1 ELSE 0 END)               AS lost,
  ROUND(
    SUM(CASE WHEN o.stage = 'closed_won' THEN 1.0 ELSE 0 END)
    / NULLIF(SUM(CASE WHEN o.stage IN ('closed_won','closed_lost')
                 THEN 1 ELSE 0 END), 0) * 100, 0)                        AS win_rate_pct,
  ROUND(AVG(CASE WHEN o.stage = 'closed_won'
                 THEN o.amount_usd END), 0)                               AS avg_won_deal_usd
FROM acme_corp.opportunities o
JOIN acme_corp.customers c ON o.customer_id = c.customer_id
GROUP BY c.segment
ORDER BY avg_won_deal_usd DESC NULLS LAST
```

**Expected result:**

| Segment | Total opps | Won | Lost | Win rate | Avg won deal (USD) |
|---|---|---|---|---|---|
| Enterprise | 4 | 3 | 0 | 100% | $1,700,000 |
| Mid-Market | 5 | 1 | 1 | 50% | $120,000 |
| SMB | 1 | 0 | 0 | — | — |

**Insight:** Enterprise deals close at a 100% win rate with an average deal size 14× larger than Mid-Market. However, SMB has no closed deals yet, suggesting the segment may still be in early exploration. The mid-market 50% win rate and a large open pipeline make it the key segment to focus on for improving conversion.

---

### 5. How are performance ratings distributed across departments in the most recent review cycle?

**Tables:** `performance_reviews`, `employees`, `departments`

```sql
SELECT
  d.name           AS department,
  pr.overall_rating,
  COUNT(*)         AS employees,
  ROUND(AVG(pr.numeric_score), 2) AS avg_score
FROM acme_corp.performance_reviews pr
JOIN acme_corp.employees e  ON pr.employee_id  = e.employee_id
JOIN acme_corp.departments d ON e.department_id = d.department_id
WHERE pr.review_period = '2024-H2'
GROUP BY d.name, pr.overall_rating
ORDER BY d.name, pr.overall_rating
```

**Expected result** *(2024-H2 cycle):*

| Department | Rating | Employees | Avg score |
|---|---|---|---|
| Customer Success | Meets | 1 | 3.70 |
| Engineering | Below | 1 | 2.40 |
| Engineering | Exceeds | 3 | 4.60 |
| Engineering | Meets | 1 | 3.80 |
| Product | Exceeds | 1 | 4.50 |
| Product | Meets | 1 | 3.90 |
| Sales | Exceeds | 1 | 4.40 |
| Sales | Meets | 1 | 3.60 |

**Insight:** Engineering has the widest performance spread — three "Exceeds" employees averaging 4.6, alongside one "Below" employee at 2.4. Sales and Product skew positive. Customer Success has only one reviewed employee, suggesting a data coverage gap to investigate.

---

## Hard

### 6. Is there a correlation between a customer's health score and their support experience?

This question requires joining customer health data against support ticket volume, CSAT scores, and resolution times to assess whether low health scores are preceded by — or reflective of — poor support outcomes.

**Tables:** `customers`, `support_tickets`

```sql
SELECT
  c.company_name                                              AS customer,
  c.segment,
  c.health_score,
  c.status                                                    AS account_status,
  COUNT(t.ticket_id)                                          AS total_tickets,
  ROUND(AVG(t.csat_score), 1)                                 AS avg_csat,
  ROUND(AVG(TIMESTAMP_DIFF(t.resolved_at, t.created_at, HOUR)), 1) AS avg_resolution_hrs
FROM acme_corp.customers c
LEFT JOIN acme_corp.support_tickets t ON c.customer_id = t.customer_id
GROUP BY c.company_name, c.segment, c.health_score, c.status
ORDER BY c.health_score ASC
```

**Expected result:**

| Customer | Segment | Health score | Account status | Tickets | Avg CSAT | Avg resolution (hrs) |
|---|---|---|---|---|---|---|
| Umbrella Biotech | Mid-Market | 30 | Churned | 1 | 3.0 | 49.0 |
| Prometheus AI | SMB | 55 | Prospect | 1 | — | — |
| Hooli Labs | Mid-Market | 64 | Active | 1 | — | — |
| Soylent Retail | Enterprise | 68 | Active | 1 | 5.0 | 31.0 |
| Initech Systems | Mid-Market | 72 | Active | 1 | 4.0 | 21.0 |
| Tyrell Data | Mid-Market | 78 | Active | 1 | 4.0 | 52.0 |
| Acme Widgets Co | Mid-Market | 81 | Active | 1 | 5.0 | 27.0 |
| Globex Financial | Enterprise | 87 | Active | 1 | 5.0 | 7.0 |
| Stark Industries | Enterprise | 91 | Active | 1 | — | — |
| Massive Dynamic | Enterprise | 95 | Active | 1 | 5.0 | 2.0 |

**Insight:** A clear positive correlation emerges between health score and support quality. The churned customer (Umbrella Biotech, score 30) had the lowest CSAT (3.0) and the longest resolution time (49 hrs) of any rated ticket. Conversely, the two highest-scoring customers — Massive Dynamic (95) and Globex Financial (87) — had perfect CSAT scores and the fastest resolution times (2 hrs and 7 hrs respectively). This suggests that support responsiveness is both a leading indicator of and contributor to customer health. Customers with CSAT scores below 4.0 or resolution times above 30 hours warrant proactive CSM outreach.

---

## Reference

| # | Question | Difficulty | Tables |
|---|---|---|---|
| 1 | ARR by product line | Easy | `subscriptions`, `products` |
| 2 | Subscriptions renewing in 90 days | Easy | `subscriptions` |
| 3 | Annual vendor spend by category | Easy | `vendor_contracts`, `vendors` |
| 4 | Win rate and deal size by segment | Intermediate | `opportunities`, `customers` |
| 5 | Performance ratings by department | Intermediate | `performance_reviews`, `employees`, `departments` |
| 6 | Health score vs support experience | Hard | `customers`, `support_tickets` |