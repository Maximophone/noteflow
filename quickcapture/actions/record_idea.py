"""Record an idea: it lands in Ideas, and IdeaTaskProcessor mines it for commitments."""

from . import Action, CaptureContext, register


@register
class RecordIdea(Action):
    key = "i"
    label = "Record idea"
    order = 20

    def run(self, ctx: CaptureContext) -> None:
        ctx.start_recording("idea", self.label)
