# Cypher Query Reference

Copy-paste Cypher queries for use in Neo4j Browser during the workshop.

---

## 1. Graph Exploration

**Count all nodes by label:**
```cypher
MATCH (n)
RETURN labels(n) AS label, count(n) AS count
ORDER BY count DESC
```

**Count all relationships by type:**
```cypher
MATCH ()-[r]->()
RETURN type(r) AS relationship_type, count(r) AS count
ORDER BY count DESC
```

**Full schema overview (labels + relationship types):**
```cypher
CALL db.schema.visualization()
```

**Show all nodes and relationships (small graphs only):**
```cypher
MATCH (n)-[r]->(m)
RETURN n, r, m
LIMIT 100
```

---

## 2. Schema Browsing

**List all tables:**
```cypher
MATCH (t:Table)
RETURN t.name AS table_name, t.description AS description
ORDER BY t.name
```

**List all columns for a specific table:**
```cypher
MATCH (t:Table {name: 'orders'})-[:HAS_COLUMN]->(c:Column)
RETURN c.name AS column,
       c.type AS data_type,
       c.nullable AS nullable,
       c.is_primary_key AS is_pk,
       c.is_foreign_key AS is_fk,
       c.description AS description
ORDER BY c.is_primary_key DESC, c.name
```

**List columns for ALL tables:**
```cypher
MATCH (t:Table)-[:HAS_COLUMN]->(c:Column)
RETURN t.name AS table, c.name AS column, c.type AS type,
       c.is_primary_key AS pk, c.is_foreign_key AS fk
ORDER BY t.name, c.name
```

**Show the full hierarchy (Database → Schema → Table → Column):**
```cypher
MATCH path = (db:Database)-[:HAS_SCHEMA]->(s:Schema)-[:HAS_TABLE]->(t:Table)-[:HAS_COLUMN]->(c:Column)
RETURN db.name AS database, s.name AS schema, t.name AS table, c.name AS column
ORDER BY t.name, c.name
LIMIT 100
```

---

## 3. Foreign Key Relationships

**Show all FK relationships:**
```cypher
MATCH (c1:Column)-[:REFERENCES]->(c2:Column)
MATCH (c1)<-[:HAS_COLUMN]-(t1:Table)
MATCH (c2)<-[:HAS_COLUMN]-(t2:Table)
RETURN t1.name AS from_table,
       c1.name AS from_column,
       t2.name AS to_table,
       c2.name AS to_column
ORDER BY from_table
```

**Show join paths from a specific table:**
```cypher
MATCH (t:Table {name: 'order_items'})-[:HAS_COLUMN]->(c:Column)-[:REFERENCES]->(c2:Column)<-[:HAS_COLUMN]-(t2:Table)
RETURN t.name AS from_table, c.name AS from_col, t2.name AS to_table, c2.name AS to_col
```

**Show the full join graph:**
```cypher
MATCH (c1:Column)-[r:REFERENCES]->(c2:Column)
RETURN c1, r, c2
```

---

## 4. Vector Index Inspection

**List all vector indexes:**
```cypher
SHOW VECTOR INDEXES
```

**Check how many nodes have embeddings (Column label):**
```cypher
MATCH (c:Column)
RETURN
  count(c) AS total_columns,
  count(c.embedding) AS columns_with_embeddings,
  count(c) - count(c.embedding) AS columns_missing_embeddings
```

**Check embedding coverage across all labels:**
```cypher
MATCH (n)
WHERE n.embedding IS NOT NULL OR n.embedding IS NULL
WITH labels(n)[0] AS label, n
RETURN label,
  count(n) AS total,
  count(n.embedding) AS with_embedding
ORDER BY label
```

**Verify embedding dimensions (sample one column):**
```cypher
MATCH (c:Column)
WHERE c.embedding IS NOT NULL
RETURN c.name AS column, size(c.embedding) AS embedding_dimensions
LIMIT 1
```

---

## 6. Business Terms (Module 3)

**Browse glossary structure:**
```cypher
MATCH (g:Glossary)-[:HAS_CATEGORY]->(c:Category)-[:HAS_BUSINESS_TERM]->(bt:BusinessTerm)
RETURN g.name AS glossary,
       c.name AS category,
       bt.name AS term,
       bt.description AS description
ORDER BY c.name, bt.name
```

**See how terms connect to columns:**
```cypher
MATCH (bt:BusinessTerm)-[:REFERS_TO]->(col:Column)<-[:HAS_COLUMN]-(t:Table)
RETURN bt.name AS business_term,
       bt.description AS term_description,
       t.name AS table,
       col.name AS column
ORDER BY bt.name
```

**Find terms by keyword (case-insensitive):**
```cypher
MATCH (bt:BusinessTerm)
WHERE toLower(bt.name) CONTAINS 'revenue' OR toLower(bt.description) CONTAINS 'revenue'
RETURN bt.name, bt.description
```

**Check BusinessTerm embedding coverage:**
```cypher
MATCH (bt:BusinessTerm)
RETURN count(bt) AS total_terms,
       count(bt.embedding) AS terms_with_embeddings
```


