# SPDX-License-Identifier: GPL-3.0-only
"""Atomic client-side export publication with native alias and state checks."""
import base64
import os
from pathlib import Path
import tempfile


def publish(snapshot,path,native,*,current,overwrite=False):
    destination=Path(path).expanduser().absolute();source=Path(native).expanduser().absolute()
    def check():
        if destination==source or destination.exists() and source.exists() and os.path.samefile(destination,source):raise ValueError('Export destination identifies the native workbook')
        if destination.exists() and not overwrite:raise FileExistsError('Export destination exists; explicitly enable overwrite')
    check();data=base64.b64decode(snapshot['file_base64'],validate=True)
    if not data:raise ValueError('Empty or invalid export response')
    descriptor,temporary=tempfile.mkstemp(prefix='.datahorizon-export-',dir=destination.parent)
    try:
        with os.fdopen(descriptor,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        current();check()
        if overwrite:os.replace(temporary,destination)
        else:
            # Atomic no-clobber publication; a concurrent creator wins safely.
            os.link(temporary,destination);os.unlink(temporary)
        return dict(path=str(destination),bytes=len(data),state=snapshot['state'],outcome='committed')
    finally:
        try:os.unlink(temporary)
        except FileNotFoundError:pass
