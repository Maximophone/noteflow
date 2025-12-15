You will be given a transcript of a meeting, the name of a participant in this meeting, and some background notes on this person. Your task is to extract specific information about this person to be appended to a markdown log.

<transcript>
{transcript_content}
</transcript>

<participant_name>
{person_name}
</participant_name>

<background_notes>
{person_content} 
</background_notes>

<meeting_date>
{meeting_date}
</meeting_date>

<meeting_title>
{meeting_title}
</meeting_title>

Analyze the transcript and extract:

1. New information: Identify any new information learned about this person that is not already present in the background notes.
2. Updates: Summarize the key updates or contributions this person made during the meeting.
3. Next steps: Determine the next steps or action items specifically assigned to or mentioned by this person.

When crafting your response:
- Do not use any section headers.
- Present the information in bullet point format.
- Be concise and informative, focusing on the most relevant and important details.
- Avoid repetition of information already present in the background notes.

Your final output should be a series of bullet points that can be directly appended to a markdown log.

