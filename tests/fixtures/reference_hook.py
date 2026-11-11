# SPDX-License-Identifier: GPL-3.0-only
def on_change(ctx, changes):
    for change in changes:
        if change['row'] == 1 and change['col'] == 1 and change['after'] and change['after']['source'] == 'go':
            ctx.set("Seen!A1", ctx.value("Total"))
