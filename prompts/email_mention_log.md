Analyze this email digest. For each person who was MENTIONED in emails but was NOT a direct correspondent (not in From/To), extract relevant notes.

<email_digest>
{email_content}
</email_digest>

<mentioned_people>
{mentioned_people_list}
</mentioned_people>

<digest_date>
{digest_date}
</digest_date>

**CRITICAL: Focus ONLY on NEW information from NEW messages.**
- Email threads include quoted previous messages - IGNORE these older quotes
- Only extract information from the LATEST message in each thread
- If a person is only mentioned in quoted/old content, omit them

**Guidelines:**
- Be CONSERVATIVE - only write what is clearly stated
- Scale output to context: many mentions = more points, one passing mention = one brief point
- Skip a person entirely if nothing meaningful (don't force content)
- These people were NOT part of the email exchange, they were discussed/mentioned

For each person, note any of:
- Why they were mentioned
- Requests or asks involving them
- Information learned about them
- Action items related to them

Return a JSON array:
```json
[
  {{
    "name": "Person Name",
    "notes": "- First point\n- Second point"
  }}
]
```

If a person has nothing meaningful to log, omit them entirely.
Return valid JSON only.
