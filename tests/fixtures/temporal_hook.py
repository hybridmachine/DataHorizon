"""Clock consistency and temporal rollback qualification fixture."""
def on_change(context, changes):
    for change in changes:
        if (change['row'],change['col']) != (1,1):continue
        sheet=change['sheet']
        if context.value(sheet,1,2) != context.value(sheet,1,3):
            raise ValueError('Clock changed within parent operation')
        context.set(sheet,2,1,'2024-02-29',kind='date')
        if context.value(sheet,1,1)==-1:
            raise ValueError('Reject after generated temporal write')
