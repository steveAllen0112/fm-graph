# fm-graph

Turn a FileMaker solution's **Database Design Report (DDR)** and **Save a Copy as XML
(SACAX)** exports into a **Neo4j property graph**, so you can answer structural questions
about the schema and logic with a one-line Cypher query instead of grepping tens of
megabytes of XML across a dozen files.

FileMaker's own DDR is an HTML/XML report and the commercial analyzers (FMPerception,
BaseElements) are themselves FileMaker files — great for browsing, awkward for
programmatic, cross-file, multi-hop questions like:

- *What actually writes `Invoice::total`? A calc, a script Set Field, an import, or is it
  hand-keyed on a layout?*
- *Trace this displayed field back through every import and calc to the raw entry field.*
- *Which relationships are **unsorted** but feed a `Last()`/`First()` calc?* (a classic
  "wrong record" bug)
- *Which buttons on any layout run script X, and what does script X call across files?*
- *Every place field Y is referenced: calcs, script steps, layouts, value lists.*

The FileMaker DDR/SACAX grammars are regular, so a parser is straightforward; the payoff is
that "who writes X / what depends on Y" becomes a graph traversal.

## Status

Working. Parses the DDR into the graph; SACAX is ingested **additively** on top of the same
nodes (UUIDs + edit provenance). Snapshots give existence *and* attribute history. See
`queries/` for the query library.

## Design

Three clean strata (the parser is generic machinery; nothing about any one solution is
baked into it):

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Mechanism | `fmgraph/parse.py`, `load.py`, `cli.py` | stream XML, build the graph, load it |
| Metadata  | `fmgraph/model.py` | the label/key schema the mechanism consumes |
| Data      | *your* DDR/SACAX files, passed as CLI args | never committed here |

### Two loaders, pick your dependency budget

- **Zero-dependency:** parse to a runnable `.cypher` file (batched `UNWIND`s) and pipe it
  through `cypher-shell`. Nothing but the Python standard library and the `cypher-shell`
  you already have with Neo4j.
- **Direct:** `pip install 'fm-graph[driver]'` to load straight over Bolt with the
  official `neo4j` driver.

### DDR vs SACAX — additive, not duplicative

The DDR builds the graph. The SACAX pass **enriches the same nodes** (matched on a stable
file + object-id key) rather than creating parallel ones: it adds each object's stable
**UUID** and **edit provenance** — the modification count, the last editor's account, and
the last-modified timestamp. Those are the attribute-change signal (see snapshots below).
Every node carries `source` (`ddr`/`sacax`) provenance, so re-ingesting updates in place
instead of duplicating.

FileMaker exports aren't always well-formed — bare `&` in exported grower/customer names,
stray XML-illegal control characters pasted into data. The reader repairs these on the way
in (illegal control chars dropped everywhere; bare `&` escaped **outside** CDATA only, so
calc bodies — where FileMaker uses `&` for concatenation — are left intact).

### Snapshots — history without duplication

Tag an ingest with `--snapshot ID` and it becomes a point in time:

```bash
fm-graph ingest --source ddr --snapshot 2026-07-22 *.xml --emit-cypher out.cypher
```

- A `FMSnapshot {id, seq, exportDate, files}` node is created for the ingest.
- Every node present gets `(node)-[:PRESENT_IN]->(snapshot)`. Re-ingesting a later export
  MERGEs the *same* nodes by key and just adds a new `PRESENT_IN` edge — **no duplication**.
- A **deletion** is the absence of a `PRESENT_IN` edge to the newer snapshot; a node's
  existence history is the ordered set of snapshots it belongs to (`node-history.cypher`,
  `deleted-since.cypher`). "Deleted" is scoped to files a snapshot actually covered, so a
  file you didn't re-ingest is never mistaken for deletions.
- Relationships (which can't point at snapshot nodes) carry a `snapshots` list plus
  `firstSnapshot`/`lastSnapshot`, appended idempotently.

**Attribute history** is captured too: volatile props (a field's `calc`, a relationship's
sort flags, a step's text, and the SACAX modification count/timestamp) are recorded on each
`PRESENT_IN` edge. The node keeps its latest values; each snapshot's edge preserves what they
were then — so `attribute-history.cypher` shows a calc changing across exports, and who
changed it (from the SACAX provenance) when. Ingest the same solution's DDR+SACAX at each
point in time and the drift is queryable.

## Graph schema (namespaced so it coexists with anything else in the DB)

Every label carries a **configurable prefix** (`--label-prefix`, default `FM`) so the
FileMaker subgraph never collides with other data in a shared database. Pick your own
(`--label-prefix FNC` → `FNCField`, `FNCScript`, …); the canned queries in `queries/` are
rewritten to match the chosen prefix by `fm-graph query`. Examples below use the `FM`
default.

Node labels (shown with the default prefix): `FMFile`, `FMBaseTable`, `FMTableOccurrence`,
`FMField`, `FMRelationship`, `FMScript`, `FMStep`, `FMLayout`, `FMValueList`,
`FMCustomFunction`, `FMExternalSource`.

Key relationships:

```
(:FMTableOccurrence)-[:BASED_ON]->(:FMBaseTable)
(:FMField)-[:IN_TABLE]->(:FMBaseTable)
(:FMField)-[:REFS]->(:FMField)              // calc dependency (from resolved DisplayCalculation chunks)
(:FMField)-[:SUMMARIZES]->(:FMField)        // summary field source
(:FMScript)-[:HAS_STEP]->(:FMStep)          // ordered by .index
(:FMStep)-[:CALLS]->(:FMScript)             // Perform Script (incl. cross-file)
(:FMStep)-[:GOES_TO]->(:FMLayout)           // Go to Layout
(:FMStep)-[:SETS]->(:FMField)               // Set Field / Set Field By Name
(:FMStep)-[:IMPORTS_INTO]->(:FMField)       // Import Records target
(:FMStep)-[:SORTS_BY]->(:FMField)
(:FMLayout)-[:BASED_ON]->(:FMTableOccurrence)
(:FMLayout)-[:SHOWS]->(:FMField)            // placed field (carries bounds)
(:FMLayout)-[:HAS_TRIGGER]->(:FMScript)     // a button/GroupButton that runs a script
(:FMRelationship)-[:LEFT|RIGHT]->(:FMTableOccurrence)   // predicate + per-side sort on the node
(:FMValueList)-[:USES_FIELD]->(:FMField)
```

Calc dependencies come **for free** from the DDR's `DisplayCalculation` chunks (already
resolved to `FieldRef`/`ScriptRef` tokens) — no FileMaker calculation-syntax parser needed.

## Install

```bash
git clone https://github.com/steveAllen0112/fm-graph
cd fm-graph
pip install -e .            # or: pip install -e '.[driver,dev]'
```

On an externally-managed Python (PEP 668), use a venv:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[driver]'
.venv/bin/fm-graph ingest ...
```

The parse + `--emit-cypher` path needs no dependencies at all. The `[driver]` extra adds the
Bolt loader (`--bolt`), which is **much** faster for large graphs — a full ~10k-node
DDR+SACAX snapshot loads in ~30s over Bolt versus minutes through `cypher-shell -f`.

## Usage

```bash
# Ingest a whole export set (DDR + SACAX together) as one snapshot, zero-dependency path.
# --source auto detects each file; DDR builds the graph, SACAX enriches the same nodes.
fm-graph ingest --source auto --snapshot 2026-07-22 \
    --emit-cypher out.cypher  *_fmp12.xml  *.xml
cypher-shell -u neo4j -p "$PW" -f out.cypher     # constraints are emitted inline

# Re-ingest a later export as a new snapshot — same nodes MERGE by key, drift is recorded.
fm-graph ingest --source auto --snapshot 2026-10-01 --emit-cypher next.cypher *.xml
cypher-shell -u neo4j -p "$PW" -f next.cypher

# Or load directly over Bolt (needs the [driver] extra; much faster than emit for big graphs):
fm-graph ingest --source auto --snapshot 2026-07-22 --bolt bolt://localhost:7687 -u neo4j *.xml

# Run a canned query (prints it; add --bolt to execute):
fm-graph query attribute-history --param 'field=Buyer Current Credit Limit' --bolt bolt://localhost:7687
```

The DDR/SACAX XML is often UTF-16; fm-graph detects and decodes it. Only nodes carrying the
configured label prefix are ever touched — a `--wipe` clears that FileMaker subgraph without
disturbing the rest of the database.

## Example queries

See `queries/`. A taste — "what writes this field, and how":

```cypher
MATCH (f:FMField {name:'total Kernel percent'})
OPTIONAL MATCH (f)<-[:SETS|IMPORTS_INTO]-(s:FMStep)<-[:HAS_STEP]-(sc:FMScript)
OPTIONAL MATCH (f)<-[:SHOWS]-(lay:FMLayout)
RETURN f.file, collect(DISTINCT sc.name) AS written_by_scripts,
       collect(DISTINCT lay.name)         AS placed_on_layouts;
```

## License

MIT — see `LICENSE`. (Copyright line and license are one-file changes if you want them
different.)
