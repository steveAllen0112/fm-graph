"""Mechanism tests for the metadata + emit layers (no database, no grammar)."""

import io

from fmgraph.model import Schema, GraphBatch, Node, Rel, Snapshot
from fmgraph.load import CypherEmitter, wipe_statement


def _sample_batch(prefix="FM"):
	sc = Schema(prefix=prefix)
	b = GraphBatch(sc)
	fk = Schema.field_key("25 Inventory", "12", "28")
	sk = Schema.script_key("25 Inventory", "41")
	b.add_node(Node("Field", fk, {"name": "total Kernel percent", "source": "ddr"}))
	b.add_node(Node("Script", sk, {"name": "O'Brien import", "source": "ddr"}))
	# Stub the same field again with a new prop -> must MERGE, not duplicate.
	b.add_node(Node("Field", fk, {"indexed": True}))
	b.add_rel(Rel("IMPORTS_INTO", sk, fk))
	return sc, b, fk, sk


def test_node_dedup_and_prop_merge():
	_, b, fk, _ = _sample_batch()
	fields = [n for n in b.nodes() if n.kind == "Field"]
	assert len(fields) == 1
	assert fields[0].props["indexed"] is True
	assert fields[0].props["name"] == "total Kernel percent"
	assert b.stats()["nodes"] == 2


def test_emit_shape_and_escaping():
	sc, b, _, _ = _sample_batch()
	out = io.StringIO()
	CypherEmitter(sc).write(b, out)
	s = out.getvalue()
	assert "CREATE CONSTRAINT fm_key" in s
	assert "MERGE (n:FM {key:" in s
	assert "SET n:FMField" in s and "SET n:FMScript" in s
	assert "MERGE (a)-[rel:IMPORTS_INTO]->(b)" in s
	assert "O\\'Brien" in s  # single-quote escaped
	# props must be a real map, not a stringified dict
	assert "props: {" in s.replace("props:{", "props: {")


def test_prefix_is_configurable():
	sc, b, _, _ = _sample_batch(prefix="FNC")
	out = io.StringIO()
	CypherEmitter(sc).write(b, out)
	s = out.getvalue()
	assert ":FNCField" in s and "MERGE (n:FNC {key:" in s
	assert ":FMField" not in s
	assert wipe_statement(sc).startswith("MATCH (n:FNC)")


def test_wipe_targets_only_prefix():
	sc = Schema(prefix="FM")
	assert "(n:FM)" in wipe_statement(sc)
	assert "IN TRANSACTIONS" in wipe_statement(sc)


def test_snapshot_emits_presence_and_membership():
	sc, b, _, _ = _sample_batch()
	snap = Snapshot(id="2026-07-22", seq="2026-07-22", exportDate="2026-07-22",
					label="Initial", files=["25 Inventory"])
	out = io.StringIO()
	CypherEmitter(sc).write(b, out, snapshot=snap)
	s = out.getvalue()
	# snapshot node
	assert "SET snap:FMSnapshot" in s
	assert "SNAP|2026-07-22" in s
	# nodes gain a PRESENT_IN edge + first/last seen
	assert "MERGE (n)-[:PRESENT_IN]->(snap)" in s
	assert "n.firstSeen = coalesce(n.firstSeen," in s
	# relationships accumulate the snapshot id idempotently
	assert "rel.snapshots = CASE WHEN '2026-07-22' IN coalesce(rel.snapshots, [])" in s


def test_no_snapshot_omits_presence():
	sc, b, _, _ = _sample_batch()
	out = io.StringIO()
	CypherEmitter(sc).write(b, out)  # no snapshot
	s = out.getvalue()
	assert "PRESENT_IN" not in s
	assert "FMSnapshot" not in s
