"""Dictate to the clipboard: live transcription, editable before you paste.

Unlike the recording actions this one never touches the pipeline — the text goes
straight to the clipboard.
"""

from . import Action, CaptureContext, register


@register
class LiveTranscript(Action):
    key = "d"
    label = "Dictate to clipboard"
    order = 30

    def run(self, ctx: CaptureContext) -> None:
        ctx.start_live_transcript(self.label)
