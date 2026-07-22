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
import re
from typing import BinaryIO, Optional

from .model import GraphBatch, Schema


# A bare '&' that is not the start of a valid entity. FileMaker exports embed
# raw user data (grower names like "Brian & Bridget Riddle") as element text
# without escaping, which is not well-formed XML. We escape those -- but only
# OUTSIDE CDATA, because calc bodies live in CDATA and FileMaker uses '&' as the
# string-concatenation operator there; escaping those would corrupt the calc.
_BARE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")

# XML 1.0 forbids these control characters ANYWHERE, even inside CDATA. Stray
# ones (e.g. a vertical tab pasted into a grower name) turn up in FileMaker data
# and make expat reject the whole file; they are noise, so we drop them.
_ILLEGAL_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_xml(text: str) -> str:
	text = _ILLEGAL_CTRL.sub("", text)
	out = []
	i = 0
	while True:
		start = text.find("<![CDATA[", i)
		if start == -1:
			out.append(_BARE_AMP.sub("&amp;", text[i:]))
			break
		out.append(_BARE_AMP.sub("&amp;", text[i:start]))
		end = text.find("]]>", start)
		if end == -1:            # unterminated CDATA: leave the remainder as-is
			out.append(text[start:])
			break
		end += 3
		out.append(text[start:end])   # CDATA verbatim
		i = end
	return "".join(out)


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
	"""Return a binary stream of well-formed UTF-8 that ElementTree can parse:
	transcode from the detected encoding, drop a BOM and the original XML
	declaration (whose stated encoding would otherwise contradict the transcoded
	bytes), and escape bare ampersands outside CDATA (FileMaker leaves raw '&' in
	exported data). The whole file is read into memory -- fine for DDR/SACAX
	sizes and the price of sanitizing."""
	enc = detect_encoding(path)
	with io.open(path, "r", encoding=enc) as f:
		text = f.read()
	if text and text[0] == "﻿":
		text = text[1:]  # stray BOM as a character (utf-16-le keeps it)
	if text.startswith("<?xml"):
		end = text.find("?>")
		if end != -1:
			text = text[end + 2 :].lstrip("\r\n")
	text = _sanitize_xml(text)
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
