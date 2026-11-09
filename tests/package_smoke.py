# SPDX-License-Identifier: GPL-3.0-only
"""Verify a relocated installation from a neutral directory, without developer overrides."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


def smoke(prefix):
    prefix=Path(prefix).resolve()
    relative="DataHorizon.app/Contents/MacOS/DataHorizon" if sys.platform=="darwin" else "bin/DataHorizon.exe" if os.name=="nt" else "bin/DataHorizon"
    service="DataHorizon.app/Contents/Resources/service/datahorizon/launch.py" if sys.platform=="darwin" else "share/datahorizon/service/datahorizon/launch.py"
    for path in prefix.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(prefix):
            raise ValueError("Installation contains a symlink outside its prefix: "+str(path.relative_to(prefix)))
    environment={k:v for k,v in os.environ.items() if not k.startswith(("PYTHON","DATAHORIZON_")) and k not in ("QT_QPA_PLATFORM","QT_PLUGIN_PATH","QT_QPA_PLATFORM_PLUGIN_PATH","QML2_IMPORT_PATH")}
    bundled = sys.platform=='darwin' and (prefix/'DataHorizon.app/Contents/Resources/runtime/bin/python3').is_file()
    if bundled:
        environment['PATH']='/usr/bin:/bin:/usr/sbin:/sbin'
    with tempfile.TemporaryDirectory(prefix="datahorizon-install-") as temp:
        relocated=Path(temp)/"relocated"; shutil.copytree(prefix,relocated,symlinks=True)
        command=[str(relocated/relative),"--smoke-test"]
        result=subprocess.run(command,cwd=temp,env=environment,capture_output=True,text=True,timeout=30)
        if result.returncode: raise RuntimeError(f"Relocated installed application failed ({result.returncode}): {result.stdout} {result.stderr}")
        python=str(relocated/'DataHorizon.app/Contents/Resources/runtime/bin/python3') if bundled else sys.executable
        api=subprocess.run([python,'-I','-B',str(Path(__file__).with_name('installed_m3_m5.py')),str((relocated/service).parent.parent)],cwd=temp,env=environment,capture_output=True,text=True,timeout=30)
        if api.returncode:raise RuntimeError('Relocated M3–M5 SDK/service checks failed: '+api.stdout+' '+api.stderr)
        if bundled:
            cli=subprocess.run([str(relocated/'DataHorizon.app/Contents/MacOS/datahorizon-cli'),'--help'],cwd=temp,env=environment,capture_output=True,text=True,timeout=10)
            if cli.returncode or 'DataHorizon' not in cli.stdout:raise RuntimeError('Bundled CLI launcher failed: '+cli.stdout+' '+cli.stderr)
        launcher=relocated/service; disabled=launcher.with_name("launch.disabled")
        launcher.rename(disabled)
        try: missing=subprocess.run(command,cwd=temp,env=environment,capture_output=True,text=True,timeout=10)
        finally: disabled.rename(launcher)
        if missing.returncode!=4 or "installation is incomplete" not in missing.stderr:
            raise RuntimeError("Missing installed launcher did not fail explicitly; development fallback may be masking an incomplete package")
    return {"platform":platform.system(),"relocated_managed_edit_calculation":True,"temporal_types_and_ten_functions":True,"m3_m5_installed_sdk_service":True,"developer_overrides_removed":True,
            "missing_service_fails_explicitly":True,"python_bundled":bundled,"developer_path_removed":bundled,
            "runtime_dependency":"Bundled checksum-pinned CPython" if bundled else "Separately installed Python",
            "clean_machine_qualified":False}


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("prefix",type=Path); parser.add_argument("--output",type=Path)
    args=parser.parse_args(); result=smoke(args.prefix)
    if args.output: args.output.write_text(json.dumps(result,indent=2)+'\n')
    print("Relocated installed application: managed edit/calculation and missing-service rejection passed")
