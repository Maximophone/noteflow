Analyze this email digest and generate a log entry for a specific correspondent.

<email_digest>
{email_content}
</email_digest>

<correspondent_name>
{person_name}
</correspondent_name>

<background_notes>
{person_content}
</background_notes>

<digest_date>
{digest_date}
</digest_date>

**CRITICAL: Focus ONLY on NEW messages from this correspondent in this digest.**
- Email threads include quoted previous messages - IGNORE these older quotes
- Only extract information from the LATEST message in each thread
- If this correspondent only appears in quoted/old messages, return empty

**Guidelines:**
- Be extremely concise - logs should be SHORTER than the email content itself
- For very short emails, just quote the key point directly if noteworthy
- Skip trivial emails (scheduling confirmations, brief acknowledgments, etc.)
- No section headers - just bullet points
- Avoid repeating information from background_notes
- Focus on: new information learned, key updates, action items, decisions

Return bullet points that can be appended to a markdown log.
If nothing meaningful to log, return "SKIP" (this is valid for trivial emails).
