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

from .model import GraphBatch, Node, Rel, Schema, Snapshot, historized


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
	if isinstance(v, dict):
		return _map_lit(v)
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
def _snapshot_props(snap: Snapshot) -> Dict[str, Any]:
	return {"key": snap.key(), "id": snap.id, "seq": snap.seq,
			"exportDate": snap.exportDate, "label": snap.label, "files": snap.files}


class CypherEmitter:
	def __init__(self, schema: Schema):
		self.schema = schema

	def write(self, batch: GraphBatch, out: TextIO, with_constraints: bool = True,
			  snapshot: Optional[Snapshot] = None) -> None:
		root = self.schema.root_label()
		out.write(f"// fm-graph load script — root label :{root}\n")
		out.write("// Generated; safe to re-run (all MERGE, idempotent).\n\n")

		if with_constraints:
			for c in _constraint_statements(self.schema):
				out.write(c + "\n")
			out.write("\n")

		# Snapshot node (once), if this ingest is snapshot-tagged.
		snap_label = self.schema.label("Snapshot")
		sid_lit = ""
		if snapshot is not None:
			sid_lit = _lit(snapshot.key())
			out.write(
				f"MERGE (snap:{root} {{key: {sid_lit}}})\n"
				f"SET snap:{snap_label}, snap += {_map_lit(_snapshot_props(snapshot))};\n\n"
			)

		# Nodes, one statement-group per specific label.
		for kind, nodes in _group_nodes(batch).items():
			specific = self.schema.label(kind)
			out.write(f"// --- {specific} ({len(nodes)}) ---\n")
			for chunk in _chunks(nodes, BATCH):
				if snapshot is not None:
					rows = ", ".join(
						_map_lit({"key": n.key, "props": n.props,
								  "hist": historized(n.props)}) for n in chunk
					)
					out.write(
						f"MATCH (snap:{snap_label} {{key: {sid_lit}}})\n"
						f"UNWIND [{rows}] AS row\n"
						f"MERGE (n:{root} {{key: row.key}})\n"
						f"SET n:{specific}, n += row.props, "
						f"n.lastSeen = {_lit(snapshot.seq)}, "
						f"n.firstSeen = coalesce(n.firstSeen, {_lit(snapshot.seq)})\n"
						f"MERGE (n)-[pi:PRESENT_IN]->(snap)\n"
						f"SET pi += row.hist;\n"
					)
				else:
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
				snap_set = ""
				if snapshot is not None:
					s = _lit(snapshot.id)
					snap_set = (
						f", rel.snapshots = CASE WHEN {s} IN coalesce(rel.snapshots, []) "
						f"THEN rel.snapshots ELSE coalesce(rel.snapshots, []) + {s} END, "
						f"rel.lastSnapshot = {_lit(snapshot.seq)}, "
						f"rel.firstSnapshot = coalesce(rel.firstSnapshot, {_lit(snapshot.seq)})"
					)
				out.write(
					f"UNWIND [{rows}] AS row\n"
					f"MATCH (a:{root} {{key: row.s}})\n"
					f"MATCH (b:{root} {{key: row.e}})\n"
					f"MERGE (a)-[rel:{rtype}]->(b)\n"
					f"SET rel += row.props{snap_set};\n"
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

	def query(self, cypher: str, **params) -> List[Dict[str, Any]]:
		kwargs = {"database": self.database} if self.database else {}
		with self._driver.session(**kwargs) as session:
			return [dict(r) for r in session.run(cypher, **params)]

	def _run(self, session, query: str, **params) -> None:
		session.run(query, **params)

	def apply(self, batch: GraphBatch, with_constraints: bool = True,
			  wipe: bool = False, snapshot: Optional[Snapshot] = None) -> None:
		root = self.schema.root_label()
		snap_label = self.schema.label("Snapshot")
		kwargs = {"database": self.database} if self.database else {}
		with self._driver.session(**kwargs) as session:
			if with_constraints:
				for c in _constraint_statements(self.schema):
					session.run(c.rstrip(";"))
			if wipe:
				session.run(wipe_statement(self.schema).rstrip(";"))
			if snapshot is not None:
				session.run(
					f"MERGE (snap:{root} {{key: $key}}) SET snap:{snap_label}, snap += $props",
					key=snapshot.key(), props=_snapshot_props(snapshot))

			for kind, nodes in _group_nodes(batch).items():
				specific = self.schema.label(kind)
				if snapshot is not None:
					q = (
						f"MATCH (snap:{snap_label} {{key: $snapkey}}) "
						f"UNWIND $rows AS row "
						f"MERGE (n:{root} {{key: row.key}}) "
						f"SET n:{specific}, n += row.props, "
						f"n.lastSeen = $seq, n.firstSeen = coalesce(n.firstSeen, $seq) "
						f"MERGE (n)-[pi:PRESENT_IN]->(snap) "
						f"SET pi += row.hist"
					)
				else:
					q = (
						f"UNWIND $rows AS row "
						f"MERGE (n:{root} {{key: row.key}}) "
						f"SET n:{specific}, n += row.props"
					)
				for chunk in _chunks(nodes, BATCH):
					if snapshot is not None:
						rows = [{"key": n.key, "props": n.props,
								 "hist": historized(n.props)} for n in chunk]
					else:
						rows = [{"key": n.key, "props": n.props} for n in chunk]
					session.run(q, rows=rows,
								snapkey=(snapshot.key() if snapshot else None),
								seq=(snapshot.seq if snapshot else None))

			for rtype, rels in _group_rels(batch).items():
				snap_set = ""
				if snapshot is not None:
					snap_set = (
						", rel.snapshots = CASE WHEN $sid IN coalesce(rel.snapshots, []) "
						"THEN rel.snapshots ELSE coalesce(rel.snapshots, []) + $sid END, "
						"rel.lastSnapshot = $seq, "
						"rel.firstSnapshot = coalesce(rel.firstSnapshot, $seq)"
					)
				q = (
					f"UNWIND $rows AS row "
					f"MATCH (a:{root} {{key: row.s}}) "
					f"MATCH (b:{root} {{key: row.e}}) "
					f"MERGE (a)-[rel:{rtype}]->(b) "
					f"SET rel += row.props{snap_set}"
				)
				for chunk in _chunks(rels, BATCH):
					rows = [{"s": r.start, "e": r.end, "props": r.props} for r in chunk]
					session.run(q, rows=rows,
								sid=(snapshot.id if snapshot else None),
								seq=(snapshot.seq if snapshot else None))
