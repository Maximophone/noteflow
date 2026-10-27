You are resolving names mentioned in a meeting transcript to notes in the user's Obsidian vault. The transcript comes from automatic speech recognition, so names are often misspelled phonetically ("Ani" for Annie, "Posia" for Pause IA).

The user is {user_name}. They attend every meeting.

**Attendees of this meeting:**
{attendees}

**Mentions to resolve.** Each mention lists what we know from past meetings:
- "history" is how the exact same spelling was resolved before.
- "candidates" are existing notes it could refer to, with how they were heard before and who they usually appear with.

{mentions}

**Transcript:**
{transcript}

**For each mention, decide what it refers to:**
- An existing candidate: give its exact wikilink. Short forms and nicknames count: "Tom Lindqvist" is [[Thomas Lindqvist]] if that note exists. A new note for someone who already has one is a mistake.
- NEW: a specific person (or organisation) with no existing note. Give the wikilink to create, with the name spelled correctly if the transcript makes it clear (e.g. "[[Elias Brandt]]"). If only a first name is known, disambiguate it from the context as "[[Firstname (Organisation or role)]]", e.g. "[[Clara (Lisbon chapter)]]".
- NONE: only when the mention is clearly not a specific person or organisation (a generic word, a mis-detection), or a first name with no plausible candidate and nothing in the context to identify them by.

Prefer your best guess to NONE. The user would rather fix a wrong link than add a missing one, and your probability is where the doubt goes. A first name or a garbled name that plausibly matches a candidate ("Ana" when there is a note for an Ana) should get that candidate, with a probability that reflects how sure you are.

Organisations are easy to mix up when their names sound alike. Use their descriptions and past aliases to tell them apart (for example a national chapter versus the global organisation).

Use the context around the mention: who is talking, what is being discussed, which attendees and organisations are involved, and who each candidate usually appears with. A first name alone ("Tom") is ambiguous unless the context or history settles it.

Then give your probability that your answer is correct, between 0 and 1. Be honest: 0.99 means you would be wrong about once in a hundred such calls. Use lower values when a first name could be several people, when the transcription is garbled, or when you are guessing between NEW and a candidate.

Also give a short description of the entity as this meeting shows it (role, organisation, relation to the user), at most 15 words, or "" if the meeting says nothing about it.

**Output:** valid JSON only, in this form:
{"resolutions": [{"id": 0, "choice": "[[Existing Note]]" | "NEW" | "NONE", "new_link": "[[Name]] if NEW else empty", "entity_type": "people" | "org", "probability": 0.0, "description": "..."}]}
Include every mention id exactly once.
