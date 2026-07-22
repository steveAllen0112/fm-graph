"""SACAX (Save a Copy as XML / FMSaveAsXML) reader — additive enrichment.

This pass does NOT rebuild the graph; it MERGEs onto the same keys the DDR pass
created (same file name + numeric object ids) and adds what only SACAX carries:
a stable object **UUID** and per-object **edit provenance** — the modification
count, the last editor's account, and the last-modified timestamp. Those are the
attribute-history signal: across snapshots you can see an object's modification
count advance and its timestamp move, and (paired with the DDR's calc text on the
same PRESENT_IN edges) see exactly when a value changed and who changed it.

Grammar (verified against real FMSaveAsXML 2.2.2.0 exports; UTF-16LE):

  <FMSaveAsXML File="X.fmp12" UUID=...>
    <Structure><AddAction> ... </AddAction><ModifyAction> ... </></Structure>
  Every object carries, as a direct child:
    <UUID modifications= userName= accountName= timestamp=>HEX</UUID>
  Catalogs (declarations) and the companion detail structures:
    BaseTableCatalog        > BaseTable id name              -> file|BT|id
    TableOccurrenceCatalog   > TableOccurrence id name        -> file|TO|id
    CustomFunctionsCatalog   > CustomFunction id name         -> file|CF|id
    ValueListCatalog         > ValueList id name              -> file|VL|id
    FieldsForTables > FieldCatalog
        > BaseTableReference id            (base-table context)
        > ObjectList > Field id name       -> file|F|<baseid>|id
    RelationshipCatalog      > Relationship id                -> file|R|id
    ScriptCatalog            > Script id name                 -> file|S|id
    LayoutCatalog            > Layout id name                 -> file|L|id
    StepsForScripts > Script
        > ScriptReference id               (script context)
        > ObjectList > Step index          -> file|ST|<scriptid>|index

References elsewhere use distinct *Reference tags (FieldReference,
TableOccurrenceReference, ScriptReference, LayoutReference), so definition tags
never collide with references — only light catalog/parent context is needed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, Optional

from .model import GraphBatch, Node, Rel, Schema


class _SACAX:
	def __init__(self, schema: Schema, batch: GraphBatch, source_tag: str,
				 exportdate: Optional[str]):
		self.schema = schema
		self.batch = batch
		self.prov = {"source": source_tag}
		if exportdate:
			self.prov["exportDate"] = exportdate
		self.file = ""
		self.cur_base_id: Optional[str] = None    # inside FieldsForTables/FieldCatalog
		self.cur_script_id: Optional[str] = None  # inside StepsForScripts/Script

	def _uuid_props(self, el: ET.Element) -> Dict[str, object]:
		u = el.find("UUID")
		p = dict(self.prov)
		if u is not None:
			p.update({
				"uuid": (u.text or None),
				"modifications": u.get("modifications"),
				"modUser": (u.get("userName") or None),
				"modAccount": u.get("accountName"),
				"modTimestamp": u.get("timestamp"),
			})
		return {k: v for k, v in p.items() if v is not None}

	def _enrich(self, kind: str, key: str, el: ET.Element, **extra) -> None:
		props = self._uuid_props(el)
		props.update({k: v for k, v in extra.items() if v is not None})
		self.batch.add_node(Node(kind, key, props))

	# -- driver -------------------------------------------------------------
	def run(self, stream) -> None:
		stack = []
		# catalog context flags keyed by the catalog element tag
		flags = {"BaseTableCatalog": False, "TableOccurrenceCatalog": False,
				 "CustomFunctionsCatalog": False, "ValueListCatalog": False,
				 "FieldsForTables": False, "RelationshipCatalog": False,
				 "ScriptCatalog": False, "LayoutCatalog": False,
				 "StepsForScripts": False}
		for event, el in ET.iterparse(stream, events=("start", "end")):
			tag = el.tag
			if event == "start":
				stack.append(tag)
				if tag == "FMSaveAsXML" and not self.file:
					name = el.get("File") or ""
					if name.lower().endswith(".fmp12"):
						name = name[:-6]
					self.file = name
					self.batch.add_node(Node("File", Schema.file_key(name),
											 {**self.prov, "name": name,
											  "uuid": el.get("UUID")}))
				elif tag in flags:
					flags[tag] = True
				elif tag == "BaseTableReference" and flags["FieldsForTables"]:
					# The FieldCatalog's base-table binding precedes its fields.
					if "FieldCatalog" in stack:
						self.cur_base_id = el.get("id")
				elif tag == "ScriptReference" and flags["StepsForScripts"]:
					self.cur_script_id = el.get("id")
				continue

			# end event
			if tag in flags:
				flags[tag] = False
			elif tag == "BaseTable" and flags["BaseTableCatalog"]:
				self._enrich("BaseTable", Schema.basetable_key(self.file, el.get("id")),
							 el, name=el.get("name"))
				el.clear()
			elif tag == "TableOccurrence" and flags["TableOccurrenceCatalog"]:
				self._enrich("TableOccurrence",
							 Schema.occurrence_key(self.file, el.get("id")),
							 el, name=el.get("name"))
				el.clear()
			elif tag == "CustomFunction" and flags["CustomFunctionsCatalog"]:
				self._enrich("CustomFunction",
							 Schema.customfunction_key(self.file, el.get("id")),
							 el, name=el.get("name"))
				el.clear()
			elif tag == "ValueList" and flags["ValueListCatalog"]:
				self._enrich("ValueList", Schema.valuelist_key(self.file, el.get("id")),
							 el, name=el.get("name"))
				el.clear()
			elif tag == "Field" and flags["FieldsForTables"] and self.cur_base_id:
				self._enrich("Field",
							 Schema.field_key(self.file, self.cur_base_id, el.get("id")),
							 el, name=el.get("name"))
				el.clear()
			elif tag == "FieldCatalog" and flags["FieldsForTables"]:
				self.cur_base_id = None
			elif tag == "Relationship" and flags["RelationshipCatalog"]:
				self._enrich("Relationship",
							 Schema.relationship_key(self.file, el.get("id")), el)
				el.clear()
			elif tag == "Script" and flags["ScriptCatalog"] and el.get("id"):
				self._enrich("Script", Schema.script_key(self.file, el.get("id")),
							 el, name=el.get("name"))
				el.clear()
			elif tag == "Layout" and flags["LayoutCatalog"] and el.get("id"):
				self._enrich("Layout", Schema.layout_key(self.file, el.get("id")),
							 el, name=el.get("name"))
				el.clear()
			elif tag == "Step" and flags["StepsForScripts"] and self.cur_script_id \
					and el.get("index") is not None:
				self._enrich("Step",
							 Schema.step_key(self.file, self.cur_script_id, el.get("index")),
							 el)
			elif tag == "Script" and flags["StepsForScripts"]:
				self.cur_script_id = None
				el.clear()
			if stack:
				stack.pop()


def parse(stream, schema: Schema, batch: GraphBatch, *, source_tag: str = "sacax",
		  exportdate: Optional[str] = None) -> None:
	_SACAX(schema, batch, source_tag, exportdate).run(stream)
