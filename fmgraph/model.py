"""The metadata stratum: the label/key schema the mechanism consumes.

Nothing here knows about any particular FileMaker solution. The parsers produce
`Node`/`Rel` values; the loaders consume them. Identity is a single string
`key` per node so the whole thing works on Neo4j Community (which has no
composite node-key constraint) and so a MERGE is a single-property lookup.

The label prefix is injected, never baked in: a `Schema(prefix="FM")` (or any
other prefix) is threaded through parse and load alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, Iterable, List, Optional


# Props whose per-snapshot values are worth keeping as history. On a
# snapshot-tagged ingest the loader copies whichever of these a node has onto
# that node's PRESENT_IN edge, so the node still holds its latest values while
# each snapshot's PRESENT_IN edge preserves what they were then. This is how
# attribute drift (a calc changing, a field being renamed, a relationship's sort
# flipping, an object's SACAX modification count/timestamp advancing) becomes
# queryable across snapshots.
HISTORIZED = frozenset({
	"name", "calc", "fieldType", "dataType",
	"leftSorted", "rightSorted", "predicate", "text",
	"source", "uuid", "modifications", "modUser", "modAccount", "modTimestamp",
})


def historized(props: Dict[str, Any]) -> Dict[str, Any]:
	"""The subset of a node's props that is tracked per snapshot."""
	return {k: v for k, v in props.items() if k in HISTORIZED}


# The bare "kinds" of node. The visible label is <prefix> + kind, e.g. "FMField".
KINDS = (
	"File",
	"BaseTable",
	"TableOccurrence",
	"Field",
	"Relationship",
	"Script",
	"Step",
	"Layout",
	"ValueList",
	"CustomFunction",
	"ExternalSource",
	"Snapshot",
)


@dataclass
class Node:
	"""A graph node. `key` is globally unique and reconstructible from a
	cross-reference, so endpoints emitted before their full definition simply
	MERGE onto the same key and get enriched later (order-independent, and what
	makes the SACAX pass additive rather than duplicative)."""

	kind: str
	key: str
	props: Dict[str, Any] = dc_field(default_factory=dict)
	# Extra labels beyond the primary <prefix><kind>, if ever needed.
	extra_labels: List[str] = dc_field(default_factory=list)


@dataclass
class Rel:
	rtype: str
	start: str  # start node key
	end: str  # end node key
	props: Dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class Snapshot:
	"""Identity of one ingest. Nodes present in it get a PRESENT_IN edge to it;
	relationships record its id in a `snapshots` list. A node/edge missing from a
	later snapshot (whose `files` cover that node's file) was deleted.

	`seq` orders snapshots (monotonic; the CLI derives it from the export date /
	an explicit value) so "latest" and "between X and Y" are well defined.
	"""

	id: str
	seq: str = ""           # sortable ordering key (e.g. ISO date/time)
	exportDate: str = ""
	label: str = ""
	files: Any = None       # list of file names this snapshot covered

	def key(self) -> str:
		return Schema.snapshot_key(self.id)


class Schema:
	"""Label naming + key construction, parameterized by a configurable prefix.

	Keys embed the FileMaker *file name* (stable across a DDR and its SACAX
	twin), which is exactly why re-ingesting or layering SACAX updates nodes in
	place instead of duplicating them.
	"""

	def __init__(self, prefix: str = "FM"):
		if not prefix or not prefix[0].isalpha():
			raise ValueError("label prefix must start with a letter")
		self.prefix = prefix

	# -- labels -------------------------------------------------------------
	def label(self, kind: str) -> str:
		if kind not in KINDS:
			raise KeyError(f"unknown node kind: {kind}")
		return f"{self.prefix}{kind}"

	def root_label(self) -> str:
		"""Shared label carried by EVERY FileMaker node (in addition to its
		specific one). A single uniqueness constraint / index on
		`root_label(key)` then makes MERGE-by-key fast regardless of the node's
		specific label, and `MATCH (n:<root>)` selects the whole subgraph for a
		clean `--wipe`."""
		return self.prefix

	def all_labels(self) -> List[str]:
		return [self.label(k) for k in KINDS]

	def labels_for(self, node: "Node") -> List[str]:
		"""[root, specific, *extra] — what the loader stamps onto a node."""
		out = [self.root_label(), self.label(node.kind)]
		for lbl in node.extra_labels:
			if lbl not in out:
				out.append(lbl)
		return out

	# -- key construction ---------------------------------------------------
	# A key is "<file>|<tag>[|<qualifier>]|<id>". Qualifiers disambiguate ids
	# that are only unique within a parent (a field id is unique within its
	# base table; a step within its script).
	@staticmethod
	def file_key(file: str) -> str:
		return f"{file}"

	@staticmethod
	def basetable_key(file: str, bt_id: str) -> str:
		return f"{file}|BT|{bt_id}"

	@staticmethod
	def occurrence_key(file: str, to_id: str) -> str:
		return f"{file}|TO|{to_id}"

	@staticmethod
	def field_key(file: str, basetable_id: str, field_id: str) -> str:
		return f"{file}|F|{basetable_id}|{field_id}"

	@staticmethod
	def script_key(file: str, script_id: str) -> str:
		return f"{file}|S|{script_id}"

	@staticmethod
	def step_key(file: str, script_id: str, index: Any) -> str:
		return f"{file}|ST|{script_id}|{index}"

	@staticmethod
	def layout_key(file: str, layout_id: str) -> str:
		return f"{file}|L|{layout_id}"

	@staticmethod
	def relationship_key(file: str, rel_id: str) -> str:
		return f"{file}|R|{rel_id}"

	@staticmethod
	def valuelist_key(file: str, vl_id: str) -> str:
		return f"{file}|VL|{vl_id}"

	@staticmethod
	def customfunction_key(file: str, cf_id: str) -> str:
		return f"{file}|CF|{cf_id}"

	@staticmethod
	def externalsource_key(file: str, xs_id: str) -> str:
		return f"{file}|X|{xs_id}"

	@staticmethod
	def snapshot_key(snap_id: str) -> str:
		return f"SNAP|{snap_id}"

	# A last-resort key for a reference that could only be resolved by name
	# (e.g. a cross-file calc ref we can't tie to an id offline). Such nodes are
	# marked unresolved=True by the parser so they're visible in the graph.
	@staticmethod
	def unresolved_field_key(file: str, table_name: str, field_name: str) -> str:
		return f"{file}|F?|{table_name}|{field_name}"


class GraphBatch:
	"""Accumulates Nodes and Rels during a parse. Nodes are de-duplicated by
	key with prop-merge (later, richer definitions win / fill blanks), so a
	stub emitted as a relationship endpoint and the full catalog entry collapse
	onto one node."""

	def __init__(self, schema: Schema):
		self.schema = schema
		self._nodes: Dict[str, Node] = {}
		self._rels: Dict[tuple, Rel] = {}

	def add_node(self, node: Node) -> Node:
		existing = self._nodes.get(node.key)
		if existing is None:
			self._nodes[node.key] = node
			return node
		# Merge: fill missing props, let non-empty new values overwrite blanks,
		# keep the most specific kind (a real catalog entry beats a stub).
		for k, v in node.props.items():
			if v is None or v == "":
				continue
			cur = existing.props.get(k)
			if cur is None or cur == "":
				existing.props[k] = v
			else:
				existing.props[k] = v
		for lbl in node.extra_labels:
			if lbl not in existing.extra_labels:
				existing.extra_labels.append(lbl)
		# A stub is created with kind matching its key tag; if a definition
		# arrives with the canonical kind, prefer it (they should already agree).
		if node.kind and existing.kind != node.kind:
			existing.kind = node.kind
		return existing

	def add_rel(self, rel: Rel) -> Rel:
		k = (rel.rtype, rel.start, rel.end)
		existing = self._rels.get(k)
		if existing is None:
			self._rels[k] = rel
			return rel
		existing.props.update({kk: vv for kk, vv in rel.props.items() if vv is not None})
		return existing

	def nodes(self) -> Iterable[Node]:
		return self._nodes.values()

	def rels(self) -> Iterable[Rel]:
		return self._rels.values()

	def stats(self) -> Dict[str, int]:
		by_kind: Dict[str, int] = {}
		for n in self._nodes.values():
			by_kind[n.kind] = by_kind.get(n.kind, 0) + 1
		by_type: Dict[str, int] = {}
		for r in self._rels.values():
			by_type[r.rtype] = by_type.get(r.rtype, 0) + 1
		return {"nodes": len(self._nodes), "rels": len(self._rels),
				**{f"node:{k}": v for k, v in sorted(by_kind.items())},
				**{f"rel:{k}": v for k, v in sorted(by_type.items())}}
