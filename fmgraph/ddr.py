"""DDR (Database Design Report XML) reader.

Grammar verified against real FileMaker 19/2025 DDR exports. Catalogs are flat
children of <File>, in this order: BaseTableCatalog, BaseDirectoryCatalog,
RelationshipGraph (TableList + RelationshipList), LayoutCatalog,
ValueListCatalog, ScriptCatalog, ... CustomFunctionCatalog,
ExternalDataSourcesCatalog.

Shape (only the parts we consume):

  BaseTable id name records > FieldCatalog > Field id dataType fieldType name
      Calculation (CDATA raw calc)
      DisplayCalculation > Chunk type="FieldRef|FunctionRef|CustomFunctionRef|NoRef"
          FieldRef        -> <Field table=TO id= name=/>          => REFS
          CustomFunctionRef -> text = custom-function name         => USES_CF
      AutoEnter [calculation=True > Calculation/DisplayCalculation] => REFS
               [lookup=True > Lookup > Field table= id=]           => LOOKS_UP
      SummaryInfo operation= > SummaryField > Field id=            => SUMMARIZES
      Storage index= global= storeCalculationResults=
  RelationshipGraph > TableList > Table id baseTableId baseTable name
      FileReference id name    (external file -> cross-file TO)
      OdbcDataSource DSN id name
  RelationshipGraph > RelationshipList > Relationship id
      Left/RightTable name= > SortList value="True|False"          (unsorted!)
      JoinPredicateList > JoinPredicate type= > Left/RightField > Field
  Layout id name width > Table id name (base occurrence)
      Object type="Field" > FieldObj > Name "TO::Field"
                                     > DDRInfo > Field id= table=   => SHOWS
      Object type="GroupButton|Button|..." > Step (Perform Script/Go to Layout)
                                                                    => HAS_TRIGGER / BUTTON_GOES_TO
  Script id name runFullAccess > StepList > Step id name enable
      StepText; Perform Script>Script id;  Go to Layout>Layout id;
      Set Field> Field(target);  Import Records> TargetFields>Field map="Import"
  ValueList id name > Source value= ; PrimaryField > Field         => USES_FIELD
  CustomFunction id name parameters > Calculation/DisplayCalculation

Ordering note: fields precede the relationship graph, so occurrence names are
unknown while a field's calc chunks are read. Such references are buffered and
resolved in a finalize pass once TableList (and the CustomFunctionCatalog) have
been seen. Everything parsed after the graph (layouts, scripts, value lists)
resolves immediately.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from .model import GraphBatch, Node, Rel, Schema


def _bool(v: Optional[str]) -> Optional[bool]:
	if v is None:
		return None
	return v.strip().lower() == "true"


class _DDR:
	def __init__(self, schema: Schema, batch: GraphBatch, source_tag: str,
				 exportdate: Optional[str]):
		self.schema = schema
		self.batch = batch
		self.prov = {"source": source_tag}
		if exportdate:
			self.prov["exportDate"] = exportdate

		self.file: str = ""
		# occurrence name -> (occ_id, base_id, home_file, external)
		self.occ: Dict[str, Tuple[str, str, str, bool]] = {}
		# (base_id, field_name) -> field_id   (this file)
		self.field_by_base_name: Dict[Tuple[str, str], str] = {}
		# (field_id, field_name) -> field_key (this file; for import targets)
		self.field_by_id_name: Dict[Tuple[str, str], str] = {}
		# custom-function name -> key (resolved at finalize)
		self.cf_by_name: Dict[str, str] = {}
		# deferred: (src_key, table_name, field_id, rtype)
		self.pending_field_refs: List[Tuple[str, str, str, str]] = []
		# deferred: (src_key, cf_name)
		self.pending_cf_refs: List[Tuple[str, str]] = []

	def _p(self, **extra) -> dict:
		d = dict(self.prov)
		d.update({k: v for k, v in extra.items() if v is not None})
		return d

	# -- key resolution -----------------------------------------------------
	def _field_key_by_occ_id(self, occ_name: str, field_id: str) -> str:
		info = self.occ.get(occ_name)
		if info:
			_, base_id, home_file, _ext = info
			return Schema.field_key(home_file, base_id, field_id)
		return Schema.unresolved_field_key(self.file, occ_name, f"#{field_id}")

	# -- fields -------------------------------------------------------------
	def _on_file(self, el: ET.Element) -> None:
		name = el.get("name") or ""
		if name.lower().endswith(".fmp12"):
			name = name[:-6]
		self.file = name
		self.batch.add_node(Node("File", Schema.file_key(name),
								 self._p(name=name, path=el.get("path"))))

	def _on_basetable(self, el: ET.Element) -> None:
		bt_id = el.get("id")
		name = el.get("name")
		key = Schema.basetable_key(self.file, bt_id)
		self.batch.add_node(Node("BaseTable", key, self._p(
			name=name, id=bt_id, file=self.file, records=el.get("records"),
			shadow=_bool(el.get("shadow")))))
		self.batch.add_rel(Rel("IN_FILE", key, Schema.file_key(self.file)))
		fc = el.find("FieldCatalog")
		if fc is not None:
			for fld in fc.findall("Field"):
				self._on_field(fld, bt_id, key)

	def _collect_chunk_refs(self, container: ET.Element, src_key: str) -> None:
		for disp in container.iter("DisplayCalculation"):
			for chunk in disp.findall("Chunk"):
				ctype = chunk.get("type")
				if ctype == "FieldRef":
					ref = chunk.find("Field")
					if ref is not None and ref.get("id"):
						self.pending_field_refs.append(
							(src_key, ref.get("table") or "", ref.get("id"), "REFS"))
				elif ctype == "CustomFunctionRef":
					if chunk.text:
						self.pending_cf_refs.append((src_key, chunk.text.strip()))

	def _on_field(self, el: ET.Element, base_id: str, base_key: str) -> None:
		fid = el.get("id")
		name = el.get("name") or ""
		ftype = el.get("fieldType") or "Normal"
		key = Schema.field_key(self.file, base_id, fid)
		calc_el = el.find("Calculation")
		calc = calc_el.text if calc_el is not None else None
		storage = el.find("Storage")
		idx = storage.get("index") if storage is not None else None
		props = self._p(
			name=name, id=fid, file=self.file, table=base_id,
			dataType=el.get("dataType"), fieldType=ftype,
			indexed=(idx not in (None, "None")) if storage is not None else None,
			global_=_bool(storage.get("global")) if storage is not None else None,
			storedCalc=_bool(storage.get("storeCalculationResults")) if storage is not None else None,
			calc=(calc.strip() if calc else None),
		)
		self.batch.add_node(Node("Field", key, props))
		self.batch.add_rel(Rel("IN_TABLE", key, base_key))
		self.field_by_base_name[(base_id, name)] = fid
		self.field_by_id_name[(fid, name)] = key

		# calc + auto-enter dependencies (deferred: occurrences unknown yet)
		self._collect_chunk_refs(el, key)
		# lookup source field
		lk = el.find("AutoEnter/Lookup/Field")
		if lk is not None and lk.get("id"):
			self.pending_field_refs.append(
				(key, lk.get("table") or "", lk.get("id"), "LOOKS_UP"))
		# summary source (same base table -> resolvable now)
		sref = el.find("SummaryInfo/SummaryField/Field")
		if sref is not None and sref.get("id"):
			self.batch.add_rel(Rel("SUMMARIZES", key,
								   Schema.field_key(self.file, base_id, sref.get("id")),
								   self._p(operation=(el.find("SummaryInfo").get("operation")))))

	# -- relationship graph -------------------------------------------------
	def _on_occurrence(self, el: ET.Element) -> None:
		occ_id = el.get("id")
		name = el.get("name")
		base_id = el.get("baseTableId")
		fileref = el.find("FileReference")
		odbc = el.find("OdbcDataSource")
		external = fileref is not None or odbc is not None
		home_file = fileref.get("name") if fileref is not None else self.file
		self.occ[name] = (occ_id, base_id, home_file, external)
		key = Schema.occurrence_key(self.file, occ_id)
		self.batch.add_node(Node("TableOccurrence", key, self._p(
			name=name, id=occ_id, file=self.file, baseTable=el.get("baseTable"),
			external=external, homeFile=home_file if external else None,
			odbcDSN=odbc.get("DSN") if odbc is not None else None)))
		self.batch.add_rel(Rel("IN_FILE", key, Schema.file_key(self.file)))
		self.batch.add_rel(Rel("BASED_ON", key, Schema.basetable_key(home_file, base_id)))
		if odbc is not None:
			xkey = Schema.externalsource_key(self.file, "odbc:" + (odbc.get("id") or odbc.get("DSN") or ""))
			self.batch.add_node(Node("ExternalSource", xkey, self._p(
				name=odbc.get("name"), kind="odbc", dsn=odbc.get("DSN"), file=self.file)))
			self.batch.add_rel(Rel("EXTERNAL_VIA", key, xkey))

	def _on_relationship(self, el: ET.Element) -> None:
		rid = el.get("id")
		key = Schema.relationship_key(self.file, rid)

		def side(t):
			if t is None:
				return None, None
			sl = t.find("SortList")
			return t.get("name"), (_bool(sl.get("value")) if sl is not None else None)

		lname, lsorted = side(el.find("LeftTable"))
		rname, rsorted = side(el.find("RightTable"))
		preds = []
		for jp in el.findall("JoinPredicateList/JoinPredicate"):
			lf = jp.find("LeftField/Field")
			rf = jp.find("RightField/Field")
			preds.append("{}.{} {} {}.{}".format(
				lname, lf.get("name") if lf is not None else "?", jp.get("type"),
				rname, rf.get("name") if rf is not None else "?"))
		self.batch.add_node(Node("Relationship", key, self._p(
			id=rid, file=self.file, leftOccurrence=lname, rightOccurrence=rname,
			leftSorted=lsorted, rightSorted=rsorted, predicate=" ; ".join(preds))))
		self.batch.add_rel(Rel("IN_FILE", key, Schema.file_key(self.file)))
		for occ_name, rtype in ((lname, "LEFT"), (rname, "RIGHT")):
			info = self.occ.get(occ_name)
			if info:
				self.batch.add_rel(Rel(rtype, key, Schema.occurrence_key(self.file, info[0])))
		for jp in el.findall("JoinPredicateList/JoinPredicate"):
			for tag, occ_name in (("LeftField", lname), ("RightField", rname)):
				f = jp.find(tag + "/Field")
				if f is not None and f.get("id"):
					self.batch.add_rel(Rel("JOIN_FIELD", key,
										   self._field_key_by_occ_id(occ_name or "", f.get("id"))))

	# -- layouts ------------------------------------------------------------
	def _on_layout(self, el: ET.Element) -> None:
		lid = el.get("id")
		key = Schema.layout_key(self.file, lid)
		self.batch.add_node(Node("Layout", key, self._p(
			id=lid, file=self.file, name=el.get("name"), width=el.get("width"))))
		self.batch.add_rel(Rel("IN_FILE", key, Schema.file_key(self.file)))
		base = el.find("Table")
		if base is not None and base.get("id"):
			self.batch.add_rel(Rel("BASED_ON", key, Schema.occurrence_key(self.file, base.get("id"))))
		for obj in el.iter("Object"):
			otype = obj.get("type")
			if otype == "Field":
				# Prefer the machine-readable DDRInfo (carries the field id, so
				# cross-file placements resolve); fall back to TO::name.
				info = obj.find("FieldObj/DDRInfo/Field")
				b = obj.find("Bounds")
				bounds = ",".join(b.get(k, "") for k in ("top", "left", "bottom", "right")) if b is not None else None
				if info is not None and info.get("id") and info.get("table"):
					tgt = self._field_key_by_occ_id(info.get("table"), info.get("id"))
					self.batch.add_rel(Rel("SHOWS", key, tgt, self._p(bounds=bounds)))
			elif otype in ("GroupButton", "Button", "ButtonBar", "PopoverButton"):
				for st in obj.iter("Step"):
					sname = st.get("name")
					sc = st.find("Script")
					if sname == "Perform Script" and sc is not None and sc.get("id"):
						self.batch.add_rel(Rel("HAS_TRIGGER", key,
											   Schema.script_key(self.file, sc.get("id")),
											   self._p(via=otype, label=obj.get("key"))))
					lay = st.find("Layout")
					if sname == "Go to Layout" and lay is not None and lay.get("id"):
						self.batch.add_rel(Rel("BUTTON_GOES_TO", key,
											   Schema.layout_key(self.file, lay.get("id")), self._p(via=otype)))

	# -- scripts ------------------------------------------------------------
	def _on_script(self, el: ET.Element) -> None:
		sid = el.get("id")
		key = Schema.script_key(self.file, sid)
		self.batch.add_node(Node("Script", key, self._p(
			id=sid, file=self.file, name=el.get("name"),
			fullAccess=_bool(el.get("runFullAccess")), inMenu=_bool(el.get("includeInMenu")))))
		self.batch.add_rel(Rel("IN_FILE", key, Schema.file_key(self.file)))
		steplist = el.find("StepList")
		if steplist is not None:
			for idx, st in enumerate(steplist.findall("Step")):
				self._on_step(st, sid, key, idx)

	def _on_step(self, st: ET.Element, script_id: str, script_key: str, idx: int) -> None:
		sname = st.get("name") or ""
		text_el = st.find("StepText")
		skey = Schema.step_key(self.file, script_id, idx)
		self.batch.add_node(Node("Step", skey, self._p(
			index=idx, file=self.file, stepId=st.get("id"), stepName=sname,
			enabled=_bool(st.get("enable")),
			text=(text_el.text if text_el is not None else None))))
		self.batch.add_rel(Rel("HAS_STEP", script_key, skey, {"index": idx}))

		sc = st.find("Script")
		if sc is not None and sc.get("id"):
			self.batch.add_rel(Rel("CALLS", skey, Schema.script_key(self.file, sc.get("id"))))
		lay = st.find("Layout")
		if lay is not None and lay.get("id"):
			self.batch.add_rel(Rel("GOES_TO", skey, Schema.layout_key(self.file, lay.get("id"))))
		if sname == "Set Field":
			tgt = st.find("Field")
			if tgt is not None and tgt.get("id"):
				self.batch.add_rel(Rel("SETS", skey,
									   self._field_key_by_occ_id(tgt.get("table") or "", tgt.get("id"))))
		if sname == "Import Records":
			for f in st.iter("Field"):
				if f.get("map") == "Import" and f.get("id"):
					k = self.field_by_id_name.get((f.get("id"), f.get("name") or ""))
					if k:
						self.batch.add_rel(Rel("IMPORTS_INTO", skey, k))

	# -- value lists / custom functions ------------------------------------
	def _on_valuelist(self, el: ET.Element) -> None:
		vid = el.get("id")
		key = Schema.valuelist_key(self.file, vid)
		src = el.find("Source")
		self.batch.add_node(Node("ValueList", key, self._p(
			id=vid, file=self.file, name=el.get("name"),
			source=(src.get("value") if src is not None else None))))
		self.batch.add_rel(Rel("IN_FILE", key, Schema.file_key(self.file)))
		f = el.find("PrimaryField/Field")
		if f is not None and f.get("id") and f.get("table"):
			self.batch.add_rel(Rel("USES_FIELD", key,
								   self._field_key_by_occ_id(f.get("table"), f.get("id"))))

	def _on_customfunction(self, el: ET.Element) -> None:
		cid = el.get("id")
		name = el.get("name") or ""
		key = Schema.customfunction_key(self.file, cid)
		calc = el.find("Calculation")
		self.batch.add_node(Node("CustomFunction", key, self._p(
			id=cid, file=self.file, name=name, parameters=el.get("parameters"),
			arity=el.get("functionArity"),
			calc=(calc.text.strip() if calc is not None and calc.text else None))))
		self.batch.add_rel(Rel("IN_FILE", key, Schema.file_key(self.file)))
		self.cf_by_name[name] = key
		self._collect_chunk_refs(el, key)  # CF may call other CFs / ref fields

	# -- driver -------------------------------------------------------------
	# Container tag -> (handler, required-ancestor catalog, excluded ancestors).
	# The context check is essential: <Script>/<Layout>/<Table>/<ValueList> also
	# appear as inline REFERENCES (inside steps, buttons, lookups, external value
	# lists) and must NOT be treated (or cleared) as definitions. Ancestor (not
	# just parent) so folder/group-nested definitions still count; the excluded
	# ancestors rule out the reference contexts.
	_CONTAINERS = {
		"BaseTable": ("_on_basetable", "BaseTableCatalog", ()),
		"Table": ("_on_occurrence", "TableList", ()),
		"Relationship": ("_on_relationship", "RelationshipList", ()),
		"Layout": ("_on_layout", "LayoutCatalog", ("Step",)),
		"Script": ("_on_script", "ScriptCatalog", ("Step",)),
		"ValueList": ("_on_valuelist", "ValueListCatalog", ("External",)),
		"CustomFunction": ("_on_customfunction", "CustomFunctionCatalog", ()),
	}

	def _finalize(self) -> None:
		for src, table, fid, rtype in self.pending_field_refs:
			tgt = self._field_key_by_occ_id(table, fid)
			if tgt != src:
				self.batch.add_rel(Rel(rtype, src, tgt))
		for src, cfname in self.pending_cf_refs:
			tgt = self.cf_by_name.get(cfname)
			if tgt:
				self.batch.add_rel(Rel("USES_CF", src, tgt))

	def run(self, stream) -> None:
		stack: List[str] = []
		for event, el in ET.iterparse(stream, events=("start", "end")):
			if event == "start":
				stack.append(el.tag)
				if el.tag == "File" and not self.file:
					self._on_file(el)
				continue
			# end event: dispatch a definition only in the right catalog context
			spec = self._CONTAINERS.get(el.tag)
			if spec:
				handler, need, excluded = spec
				ancestors = stack[:-1]  # exclude this element's own tag
				if need in ancestors and not any(x in ancestors for x in excluded):
					getattr(self, handler)(el)
					el.clear()  # reclaim only true definitions, after reading them
			if stack:
				stack.pop()
		self._finalize()


def parse(stream, schema: Schema, batch: GraphBatch, *, source_tag: str = "ddr",
		  exportdate: Optional[str] = None) -> None:
	_DDR(schema, batch, source_tag, exportdate).run(stream)
