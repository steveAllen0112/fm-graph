"""SACAX (Save a Copy as XML / FMSaveAsXML) reader.

Additive-enrichment pass: it MERGEs onto the same keys the DDR pass created
(same file name + numeric object ids), adding what only SACAX carries — stable
UUIDs and per-object edit provenance, the clean TableOccurrence/Relationship
split, and complete positional Import maps with target UUIDs.

Not yet implemented. The DDR pass builds the full graph on its own; this pass is
strictly additive and can be layered later without disturbing it.
"""

from __future__ import annotations

from typing import Optional

from .model import GraphBatch, Schema


def parse(stream, schema: Schema, batch: GraphBatch, *, source_tag: str = "sacax",
		  exportdate: Optional[str] = None) -> None:
	raise NotImplementedError(
		"SACAX ingestion is not implemented yet; use --source ddr for now.")
