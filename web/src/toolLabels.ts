// Friendly, present-tense labels for the architect's tools. Used live (the "Searching
// your notes…" status under a streaming reply) AND in the per-reply tool-call history.
// Keep in sync with the tool schemas in server/app/services/architect.py; an unlisted
// tool falls back to "Working…".
export const TOOL_LABELS: Record<string, string> = {
  // Reading notes
  find: "Finding & quoting…",
  reference_lookup: "Checking your reference library…",
  search_notes: "Searching your notes…",
  read_note: "Reading a note…",
  read_notes: "Reading notes…",
  related_notes: "Finding related notes…",
  list_recent_notes: "Looking at recent notes…",
  list_tags: "Listing tags…",
  notes_with_tag: "Finding tagged notes…",
  search_attachments: "Searching attachments…",
  read_attachment: "Reading an attachment…",
  query_sql: "Querying the database…",
  // Location & people
  current_location: "Checking your location…",
  locate_person: "Locating a person…",
  location_fixes: "Reading location history…",
  list_trips: "Listing trips…",
  trip_detail: "Reading a trip…",
  geo_distance: "Measuring distance…",
  nearby_notes: "Finding nearby notes…",
  where_was_i: "Looking up where you were…",
  time_at_place: "Calculating time at a place…",
  places_visited: "Finding places you visited…",
  distance_traveled: "Adding up distance traveled…",
  trail_summary: "Summarizing your trail…",
  entries_at_place: "Finding notes from a place…",
  reverse_geocode: "Looking up an address…",
  forward_geocode: "Looking up coordinates…",
  drug_reference: "Looking up a medication…",
  medical_reference: "Looking up a health topic…",
  list_abnormal_labs: "Finding out-of-range labs…",
  show_lab_chart: "Charting lab results…",
  lab_stat: "Checking lab values…",
  lab_value_at: "Checking lab values…",
  // Lists & tags
  read_list: "Reading a list…",
  add_list_item: "Updating a list…",
  set_item_checked: "Updating a list…",
  set_item_priority: "Updating a list…",
  add_sublist: "Updating a list…",
  set_tags: "Tagging the note…",
  // Sharing
  create_share_link: "Creating a share link…",
  create_guided_share: "Setting up a guided share…",
  create_research_share: "Setting up a research share…",
  list_share_links: "Listing share links…",
  revoke_share_link: "Revoking a share link…",
  // Writes
  log_entry: "Logging an entry…",
  propose_actions: "Drafting proposed changes…",
  // Knowledge base
  kb_coverage_check: "Checking knowledge-base coverage…",
  kb_citation_cleanup: "Cleaning up citations…",
  kb_promote_recurrences: "Finding recurring patterns…",
  kb_audit: "Auditing the knowledge base…",
  kb_taxonomy_health: "Checking taxonomy health…",
  kb_needed_links: "Finding missing links…",
  kb_research_links: "Researching references…",
  kb_read_talk: "Reading article notes…",
  kb_add_directive: "Noting a directive…",
};

// Live status form (trailing ellipsis): "Searching your notes…".
export const toolLabel = (name?: string) => (name && TOOL_LABELS[name]) || "Working…";

// Settled form for the history view — the same friendly label without the trailing
// "…" (a finished step shouldn't read as still-running).
export const toolLabelDone = (name?: string) => toolLabel(name).replace(/…$/, "");
