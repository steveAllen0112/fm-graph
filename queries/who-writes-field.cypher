// Who/what writes a field: scripts (Set Field / Import Records) and the layouts
// it is placed on (i.e. hand-keyed). An empty script list with layout hits means
// the value is entered by hand, not computed.
// Params: $field (field name). Optional: filter by file in the WHERE.
MATCH (f:FMField {name: $field})
OPTIONAL MATCH (f)<-[:SETS|IMPORTS_INTO]-(st:FMStep)<-[:HAS_STEP]-(sc:FMScript)
OPTIONAL MATCH (f)<-[:SHOWS]-(lay:FMLayout)
RETURN f.file            AS file,
       f.fieldType       AS field_type,
       f.calc            AS calc,
       collect(DISTINCT sc.name)  AS written_by_scripts,
       collect(DISTINCT lay.name) AS placed_on_layouts
ORDER BY file;
