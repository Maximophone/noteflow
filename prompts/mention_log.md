Analyze this meeting transcript. For each person who was MENTIONED but was NOT a participant, extract relevant notes.

<transcript>
{transcript_content}
</transcript>

<mentioned_people>
{mentioned_people_list}
</mentioned_people>

<meeting_title>
{meeting_title}
</meeting_title>

For each mentioned person, write bullet points covering any of:
- Why they were mentioned
- Asks or requests involving them
- Information learned about them
- Action items related to them
- Any other relevant context

**Guidelines:**
- Scale output to context: if a person is mentioned many times, write more points. If mentioned once in passing, one brief point is fine.
- Be CONSERVATIVE: only write what is clearly stated. If anything is unclear or ambiguous, omit it. Do not speculate or infer.
- Better to write too little than risk being wrong.
- Skip a person entirely if there's nothing meaningful to note (don't force content).

Return a JSON array:
```json
[
  {{
    "name": "Person Name",
    "notes": "- First point\n- Second point"
  }}
]
```

If a person has nothing meaningful to log, omit them from the array entirely.
Return valid JSON only.
