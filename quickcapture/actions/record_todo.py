"""Record a dictated todo: TodoProcessor turns it into Todoist tasks."""

from . import Action, CaptureContext, register


@register
class RecordTodo(Action):
    key = "t"
    label = "Record todo"
    order = 10

    def run(self, ctx: CaptureContext) -> None:
        ctx.start_recording("todo", self.label)
