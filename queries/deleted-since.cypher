// What was deleted: nodes present in some earlier snapshot but absent from the
// latest one -- scoped to files the latest snapshot actually covered, so a file
// that simply wasn't re-ingested is NOT mistaken for deletions.
// No params (uses the highest-seq snapshot as "latest").
MATCH (latest:FMSnapshot)
WITH latest ORDER BY latest.seq DESC LIMIT 1
MATCH (n:FM)-[:PRESENT_IN]->(:FMSnapshot)
WHERE NOT n:FMSnapshot
  AND n.file IN latest.files
  AND NOT (n)-[:PRESENT_IN]->(latest)
RETURN [l IN labels(n) WHERE l <> 'FM'][0] AS type,
       n.file AS file, n.name AS name,
       n.firstSeen AS first_seen, n.lastSeen AS last_seen_snapshot
ORDER BY file, type, name;
