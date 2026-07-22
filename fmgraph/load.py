"""Load sinks: take a `GraphBatch` and get it into Neo4j.

Two backends, differing only in their dependency budget:

  CypherEmitter — pure standard library. Writes a self-contained `.cypher`
                  script (constraints + batched UNWIND/MERGE) that any
                  `cypher-shell` can run. Data is inlined as escaped Cypher map
                  literals, so the file needs nothing else.

  BoltLoader    — uses the official `neo4j` Bolt driver (optional extra) and
                  the same statements with real parameters, so no escaping and
                  a bit faster.

Both rely on the schema's root label: every node gets `[root, specific]`, a
single uniqueness constraint sits on `root(key)`, and relationship endpoints
are matched by `(:root {key})` — indexed, label-agnostic.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, TextIO

from .model import GraphBatch, Node, Rel, Schema


BATCH = 1000


# --------------------------------------------------------------------------
# Cypher literal escaping (emit path only; the Bolt path uses parameters)
# --------------------------------------------------------------------------
def _lit(v: Any) -> str:
	"""Render a Python value as a Cypher literal."""
	if v is None:
		return "null"
	if isinstance(v, bool):
		return "true" if v else "false"
	if isinstance(v, (int, float)):
		return repr(v)
	if isinstance(v, (list, tuple)):
		return "[" + ", ".join(_lit(x) for x in v) + "]"
	s = str(v)
	s = (
		s.replace("\\", "\\\\")
		.replace("'", "\\'")
		.replace("\n", "\\n")
		.replace("\r", "\\r")
		.replace("\t", "\\t")
	)
	return f"'{s}'"


def _map_lit(d: Dict[str, Any]) -> str:
	# Cypher map keys are bare identifiers; our prop names are safe ASCII, but
	# quote-backtick any that need it.
	parts = []
	for k, v in d.items():
		key = k if k.isidentifier() else f"`{k}`"
		parts.append(f"{key}: {_lit(v)}")
	return "{" + ", ".join(parts) + "}"


def _chunks(seq: List[Any], n: int) -> Iterable[List[Any]]:
	for i in range(0, len(seq), n):
		yield seq[i : i + n]


def _constraint_statements(schema: Schema) -> List[str]:
	root = schema.root_label()
	return [
		f"CREATE CONSTRAINT {root.lower()}_key IF NOT EXISTS "
		f"FOR (n:{root}) REQUIRE n.key IS UNIQUE;"
	]


def _group_nodes(batch: GraphBatch) -> Dict[str, List[Node]]:
	by_kind: Dict[str, List[Node]] = {}
	for n in batch.nodes():
		by_kind.setdefault(n.kind, []).append(n)
	return by_kind


def _group_rels(batch: GraphBatch) -> Dict[str, List[Rel]]:
	by_type: Dict[str, List[Rel]] = {}
	for r in batch.rels():
		by_type.setdefault(r.rtype, []).append(r)
	return by_type


# --------------------------------------------------------------------------
# Emit path
# --------------------------------------------------------------------------
class CypherEmitter:
	def __init__(self, schema: Schema):
		self.schema = schema

	def write(self, batch: GraphBatch, out: TextIO, with_constraints: bool = True) -> None:
		root = self.schema.root_label()
		out.write(f"// fm-graph load script — root label :{root}\n")
		out.write("// Generated; safe to re-run (all MERGE, idempotent).\n\n")

		if with_constraints:
			for c in _constraint_statements(self.schema):
				out.write(c + "\n")
			out.write("\n")

		# Nodes, one statement-group per specific label.
		for kind, nodes in _group_nodes(batch).items():
			specific = self.schema.label(kind)
			out.write(f"// --- {specific} ({len(nodes)}) ---\n")
			for chunk in _chunks(nodes, BATCH):
				rows = ", ".join(
					_map_lit({"key": n.key, "props": n.props}) for n in chunk
				)
				out.write(
					f"UNWIND [{rows}] AS row\n"
					f"MERGE (n:{root} {{key: row.key}})\n"
					f"SET n:{specific}, n += row.props;\n"
				)
			out.write("\n")

		# Relationships, one statement-group per type.
		for rtype, rels in _group_rels(batch).items():
			out.write(f"// --- ()-[:{rtype}]->() ({len(rels)}) ---\n")
			for chunk in _chunks(rels, BATCH):
				rows = ", ".join(
					_map_lit({"s": r.start, "e": r.end, "props": r.props})
					for r in chunk
				)
				out.write(
					f"UNWIND [{rows}] AS row\n"
					f"MATCH (a:{root} {{key: row.s}})\n"
					f"MATCH (b:{root} {{key: row.e}})\n"
					f"MERGE (a)-[rel:{rtype}]->(b)\n"
					f"SET rel += row.props;\n"
				)
			out.write("\n")


def wipe_statement(schema: Schema) -> str:
	"""Delete only this tool's subgraph, in batched transactions."""
	root = schema.root_label()
	return (
		f"MATCH (n:{root}) CALL (n) {{ DETACH DELETE n }} "
		f"IN TRANSACTIONS OF 10000 ROWS;"
	)


# --------------------------------------------------------------------------
# Bolt path (optional dependency)
# --------------------------------------------------------------------------
class BoltLoader:
	def __init__(self, schema: Schema, uri: str, user: str, password: str,
				 database: Optional[str] = None):
		try:
			from neo4j import GraphDatabase  # noqa: F401
		except ImportError as e:  # pragma: no cover
			raise SystemExit(
				"The Bolt loader needs the neo4j driver: pip install 'fm-graph[driver]'\n"
				"(or use --emit-cypher to write a script for cypher-shell instead)."
			) from e
		from neo4j import GraphDatabase

		self.schema = schema
		self.database = database
		self._driver = GraphDatabase.driver(uri, auth=(user, password))

	def close(self) -> None:
		self._driver.close()

	def _run(self, session, query: str, **params) -> None:
		session.run(query, **params)

	def apply(self, batch: GraphBatch, with_constraints: bool = True,
			  wipe: bool = False) -> None:
		root = self.schema.root_label()
		kwargs = {"database": self.database} if self.database else {}
		with self._driver.session(**kwargs) as session:
			if with_constraints:
				for c in _constraint_statements(self.schema):
					session.run(c.rstrip(";"))
			if wipe:
				session.run(wipe_statement(self.schema).rstrip(";"))

			for kind, nodes in _group_nodes(batch).items():
				specific = self.schema.label(kind)
				q = (
					f"UNWIND $rows AS row "
					f"MERGE (n:{root} {{key: row.key}}) "
					f"SET n:{specific}, n += row.props"
				)
				for chunk in _chunks(nodes, BATCH):
					rows = [{"key": n.key, "props": n.props} for n in chunk]
					session.run(q, rows=rows)

			for rtype, rels in _group_rels(batch).items():
				q = (
					f"UNWIND $rows AS row "
					f"MATCH (a:{root} {{key: row.s}}) "
					f"MATCH (b:{root} {{key: row.e}}) "
					f"MERGE (a)-[rel:{rtype}]->(b) "
					f"SET rel += row.props"
				)
				for chunk in _chunks(rels, BATCH):
					rows = [{"s": r.start, "e": r.end, "props": r.props} for r in chunk]
					session.run(q, rows=rows)
