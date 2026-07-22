// The "single value pulled across an unsorted relationship" bug family. A calc
// that reads ONE value from a related table -- whether via Last()/First() or a
// bare TO::field reference -- returns an arbitrary (creation-order) record when
// the relationship side is unsorted, instead of the latest/intended one. The
// classic "pulls the wrong (older) revision" defect.
//
// Heuristic: the field's calc depends (REFS) on a field reached through a
// relationship whose relevant side is unsorted, and the calc is NOT an
// aggregation (Sum/Count/List/Max/Min/Average/GetNthRecord) -- i.e. it's a
// single-value pull. Review each hit against intent.
// No params.
MATCH (f:FMField)-[:REFS]->(dep:FMField)-[:IN_TABLE]->(:FMBaseTable)
        <-[:BASED_ON]-(occ:FMTableOccurrence)<-[:LEFT|RIGHT]-(rel:FMRelationship)
WHERE (rel.leftSorted = false OR rel.rightSorted = false)
  AND f.calc IS NOT NULL
  AND NOT (f.calc CONTAINS 'Sum (' OR f.calc CONTAINS 'Sum('
        OR f.calc CONTAINS 'Count (' OR f.calc CONTAINS 'Count('
        OR f.calc CONTAINS 'List (' OR f.calc CONTAINS 'List('
        OR f.calc CONTAINS 'Max (' OR f.calc CONTAINS 'Max('
        OR f.calc CONTAINS 'Min (' OR f.calc CONTAINS 'Min('
        OR f.calc CONTAINS 'Average (' OR f.calc CONTAINS 'Average(')
RETURN DISTINCT f.file AS file, f.name AS field,
       substring(f.calc, 0, 80) AS calc,
       dep.name AS pulls_field, occ.name AS via_occurrence,
       rel.leftSorted AS left_sorted, rel.rightSorted AS right_sorted
ORDER BY file, field;
