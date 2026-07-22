"""fm-graph: FileMaker DDR / Save-a-Copy-as-XML -> Neo4j property graph.

The package is layered so the strata stay clean:

  parse   — grammar-specific readers (ddr.py, sacax.py) that stream the XML and
            emit a format-agnostic intermediate (model.Node / model.Rel).
  model   — the label/key schema (metadata) the mechanism consumes; no file is
            baked in here.
  load    — sinks that take the intermediate and either emit a .cypher file
            (stdlib only) or write straight to Neo4j via the Bolt driver.
  cli     — wiring.

Nothing in this package hard-codes any particular database's file names, IPs,
or tables; those only ever arrive as runtime arguments.
"""

__version__ = "0.1.0"
