### CREATE vec0 Virtual Table

Source: https://github.com/asg017/sqlite-vec/blob/main/site/features/knn.md

Defines a virtual table for storing vector embeddings to enable fast KNN queries.

```APIDOC
## CREATE VIRTUAL TABLE vec0

### Description
Creates a specialized virtual table to store and index vector embeddings for high-performance similarity searching.

### Method
SQL DDL

### Endpoint
N/A (Database Table Creation)

### Parameters
#### Request Body
- **table_name** (string) - Required - The name of the virtual table.
- **columns** (definition) - Required - Column definitions including primary key and vector dimension (e.g., float[768]).

### Request Example
```sql
create virtual table vec_documents using vec0(
  document_id integer primary key,
  contents_embedding float[768]
);
```
```

--------------------------------

### Create and Query vec0 Virtual Tables

Source: https://context7.com/asg017/sqlite-vec/llms.txt

Demonstrates creating a virtual table for vector storage, inserting vector data, and performing a K-Nearest Neighbor (KNN) search.

```sql
CREATE VIRTUAL TABLE vec_documents USING vec0(
  document_id INTEGER PRIMARY KEY,
  contents_embedding float[768]
);

INSERT INTO vec_documents(document_id, contents_embedding)
VALUES
  (1, '[-0.200, 0.250, 0.341, -0.211, 0.645, 0.935, -0.316, -0.924]'),
  (2, '[0.443, -0.501, 0.355, -0.771, 0.707, -0.708, -0.185, 0.362]'),
  (3, '[0.716, -0.927, 0.134, 0.052, -0.669, 0.793, -0.634, -0.162]');

SELECT document_id, distance
FROM vec_documents
WHERE contents_embedding MATCH '[0.890, 0.544, 0.825, 0.961, 0.358, 0.0196, 0.521, 0.175]'
  AND k = 2
ORDER BY distance;
```

--------------------------------

### Sample SQL Usage for Vector Search

Source: https://github.com/asg017/sqlite-vec/blob/main/README.md

Demonstrates how to load the sqlite-vec extension, create a virtual table for storing embeddings, insert vector data, and perform a nearest neighbor search using SQL.

```sql
.load ./vec0

create virtual table vec_examples using vec0(
  sample_embedding float[8]
);

-- vectors can be provided as JSON or in a compact binary format
insert into vec_examples(rowid, sample_embedding)
  values
    (1, '[-0.200, 0.250, 0.341, -0.211, 0.645, 0.935, -0.316, -0.924]'),
    (2, '[0.443, -0.501, 0.355, -0.771, 0.707, -0.708, -0.185, 0.362]'),
    (3, '[0.716, -0.927, 0.134, 0.052, -0.669, 0.793, -0.634, -0.162]'),
    (4, '[-0.710, 0.330, 0.656, 0.041, -0.990, 0.726, 0.385, -0.958]');


-- KNN style query
select
  rowid,
  distance
from vec_examples
where sample_embedding match '[0.890, 0.544, 0.825, 0.961, 0.358, 0.0196, 0.521, 0.175]'
order by distance
limit 2;
/*
┌───────┬──────────────────┐
│ rowid │     distance     │
├───────┼──────────────────┤
│ 2     │ 2.38687372207642 │
│ 1     │ 2.38978505134583 │
└───────┴──────────────────┘
*/
```

--------------------------------

### Create and Populate vec0 Virtual Table

Source: https://github.com/asg017/sqlite-vec/blob/main/site/features/knn.md

Defines a virtual table for storing document embeddings and demonstrates how to populate it by selecting from an existing documents table.

```sql
create virtual table vec_documents using vec0(
  document_id integer primary key,
  contents_embedding float[768]
);

insert into vec_documents(document_id, contents_embedding)
  select id, embed(contents)
  from documents;
```

--------------------------------

### Python Usage of sqlite-vec

Source: https://context7.com/asg017/sqlite-vec/llms.txt

Demonstrates how to integrate sqlite-vec into a Python application using the `sqlite3` module. It shows how to load the extension, create a virtual table for vectors, serialize float vectors into a compact binary format using `struct.pack`, insert them into the table, and perform k-Nearest Neighbors (KNN) queries. This example highlights the use of `serialize_float32` for lists or direct NumPy array input (with float32 dtype).

```python
import sqlite3
import struct
import sqlite_vec

def serialize_f32(vector: list[float]) -> bytes:
    """Serialize a list of floats to compact binary format"""
    return struct.pack("%sf" % len(vector), *vector)

# Connect and load extension
db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)

# Create virtual table
db.execute("CREATE VIRTUAL TABLE vec_items USING vec0(embedding float[4])")

# Insert vectors
items = [(1, [0.1, 0.1, 0.1, 0.1]), (2, [0.2, 0.2, 0.2, 0.2]), (3, [0.3, 0.3, 0.3, 0.3])]
for id, vec in items:
    db.execute("INSERT INTO vec_items(rowid, embedding) VALUES (?, ?)",
               [id, serialize_f32(vec)])

# KNN query
query = [0.3, 0.3, 0.3, 0.3]
rows = db.execute("""
    SELECT rowid, distance FROM vec_items
    WHERE embedding MATCH ? ORDER BY distance LIMIT 3
""", [serialize_f32(query)]).fetchall()
print(rows)  # [(3, 0.0), (2, 0.4), (1, 0.8)]
```