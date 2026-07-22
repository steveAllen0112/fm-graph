// How a field's tracked attributes changed across snapshots. Reads the values
// recorded on each PRESENT_IN edge -- the calc and type from the DDR, the
// modification count / account / timestamp from SACAX -- so you see exactly what
// the field looked like at each point in time, and when/who changed it.
// Params: $field (field name).
MATCH (n:FMField {name: $field})-[pi:PRESENT_IN]->(s:FMSnapshot)
RETURN n.file AS file, n.name AS field,
       s.id AS snapshot, s.seq AS seq,
       pi.calc AS calc, pi.fieldType AS type,
       pi.modifications AS modifications, pi.modAccount AS modified_by,
       pi.modTimestamp AS modified_at
ORDER BY seq;
