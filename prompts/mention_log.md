Analyze this meeting transcript. Extract brief context about each person who was MENTIONED but was NOT a participant in the meeting.

<transcript>
{transcript_content}
</transcript>

<mentioned_people>
{mentioned_people_list}
</mentioned_people>

<meeting_title>
{meeting_title}
</meeting_title>

For each mentioned person, provide:
1. **Why mentioned**: Brief context of why they came up in the discussion (1 sentence max)
2. **Information learned**: Any new facts or updates about this person that were shared (if any)

Return a JSON array with this format:
```json
[
  {{
    "name": "Person Name",
    "why_mentioned": "Brief context",
    "information_learned": "Any new facts (or null if none)"
  }}
]
```

IMPORTANT:
- Be extremely concise - one sentence max per field
- If a person was only mentioned in passing with no meaningful context, set why_mentioned to "Briefly mentioned" and information_learned to null
- Return valid JSON only
