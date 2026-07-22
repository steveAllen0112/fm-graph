// What a script calls, transitively (Perform Script chains), and the layouts any
// step in that tree navigates to. Answers "what does this script actually touch."
// Params: $script (script name).
MATCH (s:FMScript {name: $script})
OPTIONAL MATCH (s)-[:HAS_STEP]->(:FMStep)-[:CALLS]->(called:FMScript)
OPTIONAL MATCH (s)-[:HAS_STEP]->(:FMStep)-[:GOES_TO]->(lay:FMLayout)
RETURN s.file AS file, s.name AS script,
       collect(DISTINCT called.name) AS calls_scripts,
       collect(DISTINCT lay.name)    AS goes_to_layouts;
