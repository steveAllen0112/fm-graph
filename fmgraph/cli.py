"""Command-line interface.

    fm-graph ingest  FILE...   parse DDR/SACAX and load (emit .cypher or Bolt)
    fm-graph query   NAME      run a canned query from queries/ (prefix-aware)
    fm-graph wipe              delete only the prefixed FileMaker subgraph

The label prefix (--label-prefix, default FM) is threaded everywhere so the
FileMaker subgraph never collides with other data in a shared database.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .model import Schema, Snapshot
from .load import CypherEmitter, BoltLoader, wipe_statement
from .parse import parse_files


QUERIES_DIR = Path(__file__).resolve().parent.parent / "queries"


def _resolve_password(args) -> str:
	if getattr(args, "password", None):
		return args.password
	if getattr(args, "password_env", None):
		val = os.environ.get(args.password_env)
		if not val:
			sys.exit(f"env var {args.password_env} is empty/unset")
		return val
	import getpass
	return getpass.getpass("Neo4j password: ")


def _retarget_prefix(cypher: str, prefix: str) -> str:
	"""Rewrite label references for a non-default prefix. Labels always follow a
	colon in Cypher, so replacing ':FM' covers both the root label ':FM' and
	specific labels ':FMField' without touching string literals like 'FM Grams'."""
	if prefix == "FM":
		return cypher
	return cypher.replace(":FM", f":{prefix}")


# ------------------------------------------------------------------
def _build_snapshot(args, batch) -> Optional[Snapshot]:
	if not args.snapshot:
		return None
	sid = args.snapshot
	if sid == "auto":
		sid = args.exportdate or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
	seq = args.snapshot_seq or args.exportdate or sid
	files = sorted({n.props.get("name") for n in batch.nodes()
					if n.kind == "File" and n.props.get("name")})
	return Snapshot(id=sid, seq=seq, exportDate=(args.exportdate or ""),
					label=(args.snapshot_label or ""), files=files)


def cmd_ingest(args) -> int:
	schema = Schema(prefix=args.label_prefix)
	batch = parse_files(args.files, schema, source=args.source,
						exportdate=args.exportdate)
	snapshot = _build_snapshot(args, batch)
	if args.stats:
		for k, v in batch.stats().items():
			print(f"{k:24} {v}", file=sys.stderr)
		if snapshot:
			print(f"{'snapshot':24} {snapshot.id} (seq={snapshot.seq}, "
				  f"files={len(snapshot.files or [])})", file=sys.stderr)

	if args.bolt:
		loader = BoltLoader(schema, args.bolt, args.user, _resolve_password(args),
							database=args.database)
		try:
			loader.apply(batch, with_constraints=not args.no_constraints,
						 wipe=args.wipe, snapshot=snapshot)
		finally:
			loader.close()
		print("loaded via Bolt", file=sys.stderr)
		return 0

	# Emit path.
	emitter = CypherEmitter(schema)
	out = open(args.emit_cypher, "w") if args.emit_cypher else sys.stdout
	try:
		if args.wipe:
			out.write(wipe_statement(schema) + "\n\n")
		emitter.write(batch, out, with_constraints=not args.no_constraints,
					  snapshot=snapshot)
	finally:
		if out is not sys.stdout:
			out.close()
			print(f"wrote {args.emit_cypher}", file=sys.stderr)
	return 0


def cmd_query(args) -> int:
	name = args.name if args.name.endswith(".cypher") else args.name + ".cypher"
	path = QUERIES_DIR / name
	if not path.exists():
		avail = ", ".join(sorted(p.stem for p in QUERIES_DIR.glob("*.cypher")))
		sys.exit(f"no such query '{args.name}'. available: {avail}")
	cypher = _retarget_prefix(path.read_text(), args.label_prefix)

	if not args.bolt:
		# Just print the (retargeted) query for the user to run themselves.
		sys.stdout.write(cypher)
		return 0

	schema = Schema(prefix=args.label_prefix)
	loader = BoltLoader(schema, args.bolt, args.user, _resolve_password(args),
						database=args.database)
	try:
		params = dict(p.split("=", 1) for p in (args.param or []))
		rows = loader.query(cypher, **params)
	finally:
		loader.close()
	for row in rows:
		print(row)
	return 0


def cmd_wipe(args) -> int:
	schema = Schema(prefix=args.label_prefix)
	stmt = wipe_statement(schema)
	if not args.bolt:
		print(stmt)
		return 0
	loader = BoltLoader(schema, args.bolt, args.user, _resolve_password(args),
						database=args.database)
	try:
		loader.query(stmt.rstrip(";"))
	finally:
		loader.close()
	print(f"wiped :{schema.root_label()} subgraph", file=sys.stderr)
	return 0


# ------------------------------------------------------------------
def _add_conn(p: argparse.ArgumentParser) -> None:
	p.add_argument("--label-prefix", default="FM",
				   help="label prefix for the FileMaker subgraph (default: FM)")
	p.add_argument("--bolt", help="Bolt URI (e.g. bolt://localhost:7687); "
				   "omit to use the zero-dependency emit path")
	p.add_argument("-u", "--user", default="neo4j")
	p.add_argument("-p", "--password", help="(discouraged: visible in ps)")
	p.add_argument("--password-env", help="name of env var holding the password")
	p.add_argument("--database", help="target database (Enterprise; ignored on Community)")


def build_parser() -> argparse.ArgumentParser:
	ap = argparse.ArgumentParser(prog="fm-graph",
								 description="FileMaker DDR/SACAX -> Neo4j graph")
	sub = ap.add_subparsers(dest="cmd", required=True)

	ing = sub.add_parser("ingest", help="parse DDR/SACAX and load")
	ing.add_argument("files", nargs="+", help="DDR/SACAX .xml exports")
	ing.add_argument("--source", choices=["auto", "ddr", "sacax"], default="auto")
	ing.add_argument("--emit-cypher", metavar="FILE",
					 help="write a .cypher load script (default: stdout)")
	ing.add_argument("--wipe", action="store_true",
					 help="clear the prefixed subgraph before loading")
	ing.add_argument("--no-constraints", action="store_true")
	ing.add_argument("--exportdate", help="stamp nodes/rels with this export date")
	ing.add_argument("--snapshot", metavar="ID",
					 help="tag this ingest as a snapshot: nodes get PRESENT_IN "
						  "this snapshot (no duplication on re-ingest); use 'auto' "
						  "to derive the id from --exportdate/now")
	ing.add_argument("--snapshot-seq", metavar="SEQ",
					 help="sortable ordering key for the snapshot (default: "
						  "--exportdate or the id); defines 'latest'/'between'")
	ing.add_argument("--snapshot-label", help="human label for the snapshot")
	ing.add_argument("--stats", action="store_true", help="print batch counts")
	_add_conn(ing)
	ing.set_defaults(func=cmd_ingest)

	q = sub.add_parser("query", help="emit/run a canned query")
	q.add_argument("name", help="query name (see queries/)")
	q.add_argument("--param", action="append", metavar="K=V",
				   help="query parameter (repeatable)")
	_add_conn(q)
	q.set_defaults(func=cmd_query)

	w = sub.add_parser("wipe", help="delete only the prefixed subgraph")
	_add_conn(w)
	w.set_defaults(func=cmd_wipe)

	return ap


def main(argv: Optional[List[str]] = None) -> int:
	args = build_parser().parse_args(argv)
	return args.func(args)


if __name__ == "__main__":
	raise SystemExit(main())
