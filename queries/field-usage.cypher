// Everywhere a field is used: calcs that read it, scripts that set/import it,
// layouts that show it, value lists and relationships built on it. The blast
// radius before you change or delete a field.
// Params: $field (field name).
MATCH (f:FMField {name: $field})
OPTIONAL MATCH (f)<-[:REFS]-(rf:FMField)
OPTIONAL MATCH (f)<-[:SETS|IMPORTS_INTO]-(:FMStep)<-[:HAS_STEP]-(sc:FMScript)
OPTIONAL MATCH (f)<-[:SHOWS]-(lay:FMLayout)
OPTIONAL MATCH (f)<-[:USES_FIELD]-(vl:FMValueList)
OPTIONAL MATCH (f)<-[:JOIN_FIELD]-(rel:FMRelationship)
RETURN f.file AS file, f.name AS field,
       collect(DISTINCT rf.name)  AS referenced_by_calcs,
       collect(DISTINCT sc.name)  AS written_by_scripts,
       collect(DISTINCT lay.name) AS shown_on_layouts,
       collect(DISTINCT vl.name)  AS in_value_lists,
       collect(DISTINCT rel.predicate) AS in_relationships;
