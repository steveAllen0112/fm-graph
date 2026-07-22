"""Front-end: encoding detection, format sniffing, and dispatch.

FileMaker's DDR export is commonly UTF-16 (with a BOM); SACAX is usually UTF-8.
Both are streamed with `xml.etree.ElementTree.iterparse` so even a 50 MB file
never lands in memory whole. The grammar-specific work lives in `ddr.py` and
`sacax.py`, each exposing:

    parse(source, schema, batch, *, source_tag, exportdate) -> None

populating the shared `GraphBatch`.
"""

from __future__ import annotations

import io
import os
from typing import BinaryIO, Optional

from .model import GraphBatch, Schema


# ------------------------------------------------------------------
# Encoding
# ------------------------------------------------------------------
def detect_encoding(path: str) -> str:
	with open(path, "rb") as f:
		head = f.read(4)
	if head[:2] == b"\xff\xfe":
		return "utf-16-le" if head[2:4] != b"\x00\x00" else "utf-32-le"
	if head[:2] == b"\xfe\xff":
		return "utf-16-be"
	if head[:3] == b"\xef\xbb\xbf":
		return "utf-8-sig"
	return "utf-8"


def open_utf8(path: str) -> BinaryIO:
	"""Return a binary stream of UTF-8 bytes that ElementTree can parse,
	transcoding UTF-16/32 on the fly and dropping the original XML declaration
	(whose stated encoding would otherwise contradict the transcoded bytes)."""
	enc = detect_encoding(path)
	if enc in ("utf-8", "utf-8-sig"):
		# expat handles a UTF-8 BOM and the declaration itself.
		return open(path, "rb")

	# Transcode. Read as text in the detected encoding, strip a leading XML
	# declaration, and hand back UTF-8 bytes.
	with io.open(path, "r", encoding=enc) as f:
		text = f.read()
	if text.startswith("<?xml"):
		end = text.find("?>")
		if end != -1:
			text = text[end + 2 :].lstrip("\r\n")
	return io.BytesIO(text.encode("utf-8"))


# ------------------------------------------------------------------
# Format sniffing
# ------------------------------------------------------------------
def detect_format(path: str) -> str:
	"""'ddr' or 'sacax', by sniffing the opening markup. DDR reports declare a
	FileMaker Pro report / FMDynamicTemplate root; SACAX uses the
	FMSaveAsXML / fmxmlsnippet family. Falls back to 'ddr'."""
	stream = open_utf8(path)
	try:
		head = stream.read(4096)
	finally:
		stream.close()
	text = head.decode("utf-8", errors="replace")
	low = text.lower()
	if "fmsaveasxml" in low or "fmxmlsnippet" in low:
		return "sacax"
	if "fmdynamictemplate" in low or "database design report" in low or "<fmpreport" in low:
		return "ddr"
	# Default: treat as DDR (the richer where-used report).
	return "ddr"


# ------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------
def parse_file(path: str, schema: Schema, *, source: str = "auto",
			   exportdate: Optional[str] = None) -> GraphBatch:
	from . import ddr, sacax

	fmt = detect_format(path) if source == "auto" else source
	batch = GraphBatch(schema)
	stream = open_utf8(path)
	try:
		if fmt == "sacax":
			sacax.parse(stream, schema, batch, source_tag="sacax", exportdate=exportdate)
		else:
			ddr.parse(stream, schema, batch, source_tag="ddr", exportdate=exportdate)
	finally:
		stream.close()
	return batch


def parse_files(paths, schema: Schema, *, source: str = "auto",
				exportdate: Optional[str] = None) -> GraphBatch:
	"""Parse many files into ONE batch (they share keys, so cross-file edges —
	external-source script calls, imports — resolve across the set)."""
	from . import ddr, sacax

	batch = GraphBatch(schema)
	for path in paths:
		fmt = detect_format(path) if source == "auto" else source
		stream = open_utf8(path)
		try:
			mod = sacax if fmt == "sacax" else ddr
			mod.parse(stream, schema, batch, source_tag=fmt if source != "auto" else fmt,
					  exportdate=exportdate)
		finally:
			stream.close()
	return batch
