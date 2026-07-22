// List every snapshot with how many nodes it contains, newest first.
MATCH (s:FMSnapshot)
OPTIONAL MATCH (n:FM)-[:PRESENT_IN]->(s)
RETURN s.id AS snapshot, s.seq AS seq, s.exportDate AS exportDate,
       s.label AS label, s.files AS files, count(n) AS nodes
ORDER BY seq DESC;
