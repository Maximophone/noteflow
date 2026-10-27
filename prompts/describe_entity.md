You are writing a one-line description of an entity from the user's Obsidian vault. An entity resolver will show this line next to the entity's name when deciding who or what a name in a meeting transcript refers to. Names in transcripts are often just a first name ("Tom") or misspelled, and several entities have similar names.

The user is {user_name}.

**Entity:** {link}

**Entities with similar names that this one must be told apart from:**
{similar}

**The entity's note:**
{note}

Write the description so that someone reading a transcript could tell this entity apart from the similar ones: role, organisation or chapter, city or country, what they work on with the user, and anything distinctive. Prefer stable facts over one-off events. Leave out personal or sensitive details (health, conflicts, accusations, contact details): the line only needs to identify the entity. At most 25 words. No full sentences needed. Do not invent anything that is not in the note; if the note says almost nothing, say what little it does.

**Output:** valid JSON only: {"description": "..."}
