"""Trusted containment fixture: failures occur after a transactional edit."""
import os


def on_change(context, changes):
    for change in changes:
        if (change['row'], change['col']) != (1, 1):
            continue
        sheet = change['sheet']
        value = context.value(sheet, 1, 1)
        context.set(sheet, 1, 2, str(value * 2))
        if value == -1:
            raise ValueError('failure after generated edit')
        if value == -2:
            while True:
                pass
        if value == -3:
            os._exit(17)
