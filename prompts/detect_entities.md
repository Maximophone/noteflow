You are an expert entity detection system. Your task is to list the named entities mentioned in a transcript. A later step decides which note each one links to.

You will be provided with:
1. The entities already known, each with the spellings it was heard as before (People, Organisations).
2. The transcript text. It comes from speech recognition, so names are often misspelled.

Your goal is to detect:
- **People**: Names of specific individuals (new or existing).
- **Organisations**: ONLY organisations that already appear in the "Known Entities" list below (or are spellings of one). Do NOT detect any organisation that is not already in that list, even if it would otherwise be a plausible entity.

Do NOT detect any other type of entity (concepts, projects, locations, etc.).

**Guidelines:**
- **Be Specific:** Do NOT detect generic terms like "AGI", "machine learning", "the team", "the company", "the model".
- **Ignore High-Level Concepts:** Do NOT detect names of countries ("UK", "France", "USA"), continents ("Europe", "Asia"), or major geographic regions unless they are the specific topic of discussion.
- **Ignore Major Platforms:** Do NOT detect ubiquitous platforms like "YouTube", "Google", "Twitter", "Android", "iOS", "Windows" unless the conversation is specifically about the company/entity itself (e.g. "Google's strategy").
- **Misspellings count:** A garbled spelling of a known entity ("Posia" for Pause IA) is a mention of it. Report it as it is spelled in the transcript.
- **Organisations:** only report an organisation if it is one of the known organisations (or a spelling of one). Skip any other organisation entirely.
- **Every spelling:** If the same entity appears under several spellings, report each spelling separately.
- **Granularity:** Focus on entities that would be useful to link in a knowledge base (Obsidian vault). We want *specific* people, and only organisations that are already known.

**Known Entities:**
{entity_references}

**Transcript:**
{transcript}

**Output Format:**
Return a JSON object with a single key "entities" containing a list of objects. Each object must have:
- `detected_name`: The exact text as it appears in the transcript.
- `entity_type`: One of "people", "org". Only emit "org" entries that correspond to a known organisation.

Response must be valid JSON only.
