You are summarizing a meeting. You have access to:
1. The full meeting transcript with frontmatter (contains date, title, speakers)
2. Context from recent meetings in the same period
3. Background notes on each attendee

<transcript>
{transcript}
</transcript>

<monthly_meetings_context>
{monthly_index}
</monthly_meetings_context>

<attendee_notes>
{attendee_notes}
</attendee_notes>

Generate a structured meeting summary with these sections:

## Summary
Provide a 2-4 sentence semantic overview of what was discussed. Focus on the purpose and outcomes of the meeting.

## Decisions Made
List any decisions that were made during the meeting. If no explicit decisions, write "No explicit decisions recorded."
- Format: Clear statement of the decision with brief context

## Action Items
List action items with owner and deadline if mentioned. No checkboxes.
- Format: [[Person Name]] Task description (by DATE if specified)

## Key Topics
List the main themes/topics discussed as hashtags (e.g., #product-strategy, #hiring, #q1-planning)

## Per-Person Contributions
For each meeting participant, briefly summarize their key contributions or talking points:

### [[Person Name]]
- Brief bullet points of what they discussed/contributed

---

Output ONLY the markdown content for these sections. Do not include any preamble or explanation.
