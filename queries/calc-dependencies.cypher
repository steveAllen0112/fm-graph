// Transitive calculation dependencies of a field: every field its calc reads,
// directly or through intermediate calcs (the REFS chain), plus any custom
// functions used. Answers "if I change X, what feeds it / what breaks."
// Params: $field (field name).
MATCH (f:FMField {name: $field})
OPTIONAL MATCH path = (f)-[:REFS*1..12]->(dep:FMField)
WITH f, collect(DISTINCT dep.name) AS depends_on
OPTIONAL MATCH (f)-[:USES_CF]->(cf:FMCustomFunction)
RETURN f.file AS file, f.name AS field, f.calc AS calc,
       depends_on,
       collect(DISTINCT cf.name) AS custom_functions;
