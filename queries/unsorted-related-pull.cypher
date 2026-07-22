// The "single value pulled across an unsorted relationship" bug family. A calc
// that reads ONE value from a related table -- whether via Last()/First() or a
// bare TO::field reference -- returns an arbitrary (creation-order) record when
// the relationship side is unsorted, instead of the latest/intended one. The
// classic "pulls the wrong (older) revision" defect.
//
// Heuristic: the field's calc depends (REFS) on a field reached through a
// relationship, the calc is NOT an aggregation (Sum/Count/List/Max/Min/Average)
// -- i.e. a single-value pull -- and the side of the relationship you pull FROM
// (the one the referenced field lives on) is unsorted. Only that side's sort
// matters: pulling from a side sorted descending-by-date is CORRECT, not a bug,
// so this checks the pulled-from side specifically (not "either side").
// Review each hit against intent. No params.
MATCH (f:FMField)-[:REFS]->(dep:FMField)-[:IN_TABLE]->(:FMBaseTable)
        <-[:BASED_ON]-(occ:FMTableOccurrence)
MATCH (rel:FMRelationship)-[side:LEFT|RIGHT]->(occ)
WITH f, dep, occ, rel,
     CASE type(side) WHEN 'LEFT' THEN rel.leftSorted ELSE rel.rightSorted END
       AS pulled_side_sorted
WHERE pulled_side_sorted = false
  AND f.calc IS NOT NULL
  AND NOT (f.calc CONTAINS 'Sum (' OR f.calc CONTAINS 'Sum('
        OR f.calc CONTAINS 'Count (' OR f.calc CONTAINS 'Count('
        OR f.calc CONTAINS 'List (' OR f.calc CONTAINS 'List('
        OR f.calc CONTAINS 'Max (' OR f.calc CONTAINS 'Max('
        OR f.calc CONTAINS 'Min (' OR f.calc CONTAINS 'Min('
        OR f.calc CONTAINS 'Average (' OR f.calc CONTAINS 'Average(')
RETURN DISTINCT f.file AS file, f.name AS field,
       substring(f.calc, 0, 80) AS calc,
       dep.name AS pulls_field, occ.name AS via_unsorted_occurrence
ORDER BY file, field;
