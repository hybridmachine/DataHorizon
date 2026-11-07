"""Read-only native-file inspection; never repairs data or activates workbook code."""
from .engine import Workbook


def inspect_native(path):
    book=Workbook(path,readonly=True)
    try:
        result=book.info()
        return {"path":result["path"],"workbook_id":result["id"],"format_version":result["format_version"],
                "sheets":result["sheets"],"sqlite_integrity":"ok","readonly":True,
                "report":["SQLite quick_check and native schema/catalog/coordinate validation passed within the opening budget.",
                          "No code, change handlers, recalculation or repairs were run. Cell inputs and formula/export compatibility have not been exhaustively checked.",
                          "Inspection opens no persistent client handle. Retain the original; open a native copy for further recovery work."]}
    finally:
        book.close()
