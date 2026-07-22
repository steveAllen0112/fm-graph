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

Early. Parses the DDR into the graph; SACAX is ingested **additively** on top of the same
nodes (see below). See `queries/` for the working query library and `CHANGELOG`/issues for
what's landed.

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
key) rather than creating parallel ones: it adds UUIDs, precise layout geometry, and — the
practical win — **structured Import Records field maps** (the DDR only lists imports by
positional "Source field N", which can't be resolved offline). Anything present only in
SACAX becomes new nodes tagged with their source. Every node and relationship carries
`source` (`ddr`/`sacax`) and `exportDate` provenance, so re-ingesting a newer export
updates in place instead of duplicating.

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

This tracks *existence* history. It does not, by itself, preserve per-snapshot *attribute*
history (a node holds its latest calc/props); capturing attribute drift is a planned opt-in
that moves volatile props onto the `PRESENT_IN` edge.

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

## Usage

```bash
# Parse a DDR export set and emit a load script (zero-dependency path):
fm-graph ingest --source ddr --emit-cypher out.cypher  "MyFile_fmp12.xml" ...
cat constraints.cypher out.cypher | cypher-shell -u neo4j -p "$PW"

# Layer the SACAX export on top (additive enrichment):
fm-graph ingest --source sacax --emit-cypher enrich.cypher  "MyFile.xml" ...
cypher-shell -u neo4j -p "$PW" < enrich.cypher

# Or load directly over Bolt (needs the [driver] extra):
fm-graph ingest --source auto --bolt bolt://localhost:7687 -u neo4j  *.xml
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
