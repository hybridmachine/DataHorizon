"""Run the installed application without creating persistent user workbooks."""

import os
from pathlib import Path
import subprocess
import sys

prefix = Path(sys.argv[1]).resolve()
relative = "DataHorizon.app/Contents/MacOS/DataHorizon" if sys.platform == "darwin" else "bin/DataHorizon.exe" if os.name == "nt" else "bin/DataHorizon"
environment=dict(os.environ)
environment.pop("QT_QPA_PLATFORM",None)
result = subprocess.run([str(prefix / relative), "--smoke-test"], env=environment,
                        capture_output=True, text=True, timeout=30)
if result.returncode:
    print(result.stdout, result.stderr, file=sys.stderr)
    raise SystemExit(result.returncode)
print("Installed application: managed edit and calculation passed")
