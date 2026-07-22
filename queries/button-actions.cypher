// Every button action on a layout: which scripts its buttons run (HAS_TRIGGER)
// and which layouts its buttons navigate to directly (BUTTON_GOES_TO). Useful
// for "why does this button land me on the wrong layout / wrong found set."
// Params: $layout (layout name).
MATCH (l:FMLayout {name: $layout})
OPTIONAL MATCH (l)-[t:HAS_TRIGGER]->(sc:FMScript)
OPTIONAL MATCH (l)-[g:BUTTON_GOES_TO]->(dest:FMLayout)
RETURN l.file AS file, l.name AS layout,
       collect(DISTINCT sc.name)   AS buttons_run_scripts,
       collect(DISTINCT dest.name) AS buttons_go_to_layouts;
