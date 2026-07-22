// The snapshot history of the fields matching a name: which snapshots each was
// present in, and its first/last seen ordering keys. Gaps in the snapshot list
// (relative to `snapshots.cypher`) mean the object was deleted then re-added.
// Params: $field (field name).
MATCH (n:FMField {name: $field})-[:PRESENT_IN]->(s:FMSnapshot)
WITH n, s ORDER BY s.seq
RETURN n.file AS file, n.name AS field,
       n.firstSeen AS first_seen, n.lastSeen AS last_seen,
       collect(s.id) AS present_in_snapshots;
