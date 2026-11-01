"""Opt-in fixture, never a built-in business rule or workbook trust decision."""


def on_change(context, changes):
    print("Validation fixture invocation")
    for change in changes:
        if (change["row"], change["col"]) == (1, 1):
            value = context.value(change["sheet"], 1, 1)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError("Fixture expects a nonnegative number in A1")
            context.set(change["sheet"], 1, 2, str(value * 2))
