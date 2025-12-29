Analyze this email digest. For each DIRECT CORRESPONDENT (people in From/To lines), extract relevant notes about their communications.

<email_digest>
{email_content}
</email_digest>

<correspondents>
{correspondents_list}
</correspondents>

<digest_date>
{digest_date}
</digest_date>

**CRITICAL: Focus ONLY on NEW messages from each correspondent.**
- Email threads include quoted previous messages - IGNORE these older quotes
- Only extract information from the LATEST message in each thread
- If a correspondent only appears in quoted/old messages, skip them

**Guidelines:**
- Be extremely CONCISE - logs should be SHORTER than the email content
- For very short/trivial emails (scheduling confirmations, brief acknowledgments), skip them
- For meaningful emails, focus on: key updates, action items, decisions, new information
- No section headers - just bullet points
- Scale output to content: brief email = 1 point, detailed email = 2-3 points max

Return a JSON array:
```json
[
  {{
    "name": "Person Name",
    "notes": "- First point\n- Second point"
  }}
]
```

If a correspondent has nothing meaningful to log (trivial emails only), omit them entirely.
Return valid JSON only.
