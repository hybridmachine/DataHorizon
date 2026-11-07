"""Small, deliberately bounded spreadsheet parser. Never evaluates Python source."""

from decimal import Context, Decimal, InvalidOperation, localcontext, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN
import math
import re
import time

SUPPORTED_FUNCTIONS = {"SUM", "IF", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "AND", "OR", "NOT", "IFERROR", "ABS", "ROUND", "ROUNDUP", "ROUNDDOWN"}
CELL_ERRORS = {"#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NUM!", "#N/A", "#NULL!"}
NUMBER_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")

MAX_ROWS, MAX_COLS = 1_048_576, 16_384
REF = r"(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z_0-9]*)!)?\$?[A-Za-z]{1,3}\$?[1-9][0-9]*"
TOKEN = re.compile(
    rf'\s*(?:(?P<ref>{REF})(?![A-Za-z_0-9]|\s*\()|'
    r'(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)|'
    r'(?P<string>"(?:[^"]|"")*")|(?P<error>\#REF!)|'
    r'(?P<name>[A-Za-z_][A-Za-z_0-9]*)|(?P<op><=|>=|<>|[+*/^(),:=<>-]))'
)
ADDRESS = re.compile(r"(\$?)([A-Za-z]{1,3})(\$?)([1-9][0-9]*)\Z")
QUOTED_TOKEN = re.compile(r''' "(?:[^"]|"")*" | '(?:[^']|'')*' ''', re.VERBOSE)
ERROR_LITERAL = re.compile(r"#(?:REF!|DIV/0!|VALUE!|NAME\?|NUM!|N/A|NULL!|CYCLE!|LIMIT!)", re.IGNORECASE)


class FormulaError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


def column_name(col):
    out = ""
    while col:
        col, digit = divmod(col - 1, 26)
        out = chr(65 + digit) + out
    return out


def address(row, col):
    return f"{column_name(col)}{row}"


def coordinates(source):
    match = ADDRESS.fullmatch(source)
    if not match:
        raise FormulaError("#REF!", "Expected an A1 address")
    col = 0
    for char in match[2].upper():
        col = col * 26 + ord(char) - 64
    if len(match[4]) > len(str(MAX_ROWS)):
        raise FormulaError("#REF!", "Address exceeds the supported grid")
    row = int(match[4])
    if row > MAX_ROWS or col > MAX_COLS:
        raise FormulaError("#REF!", "Address exceeds the supported grid")
    return row, col


def split_ref(source):
    if "!" in source:
        sheet, source = source.rsplit("!", 1)
        if sheet.startswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    else:
        sheet = None
    return sheet, *coordinates(source)


def has_sheet_reference(source):
    """Ignore error literals and quotes, retaining a quoted sheet's separator.

    An incomplete quoted token stays conservative: an unclassified ! blocks
    structural operations until the formula can be reviewed.
    """
    pos = 1
    while pos < len(source):
        if source[pos] == "!":
            return True
        if source[pos] == "#" and (token := ERROR_LITERAL.match(source, pos)):
            pos = token.end()
        elif source[pos] in ("\"", "'"):
            token = QUOTED_TOKEN.match(source, pos)
            if not token:
                return "!" in source[pos:]
            pos = token.end()
        else:
            pos += 1
    return False


def translate(source, rows, cols):
    """Copy/fill offsets affect relative components; string literals are untouched."""
    if not source.startswith("="):
        return source
    out, pos = ["="], 1
    while pos < len(source):
        if source[pos:].isspace():
            out.append(source[pos:])
            break
        match = TOKEN.match(source, pos)
        if not match:
            # Preserve an unsupported formula unchanged rather than corrupting it.
            raise FormulaError("#VALUE!", "Cannot translate unsupported formula syntax")
        token = match.group(0)
        if match.lastgroup == "ref":
            ref = match["ref"]
            prefix, cell = ref.rsplit("!", 1) if "!" in ref else (None, ref)
            parts = ADDRESS.fullmatch(cell)
            row, col = coordinates(cell)
            row += 0 if parts[3] else rows
            col += 0 if parts[1] else cols
            if not (1 <= row <= MAX_ROWS and 1 <= col <= MAX_COLS):
                new = "#REF!"
            else:
                new = f"{parts[1]}{column_name(col)}{parts[3]}{row}"
                if prefix:
                    new = prefix + "!" + new
            token = token[:len(token) - len(ref)] + new
        out.append(token)
        pos = match.end()
    return "".join(out)


def rename_sheet_references(source, old_name, new_name, *, target_id=None, new_id=None):
    """Retarget qualified names; copies can also retarget explicit self UUIDs."""
    parser = Parser(source[1:], track_spans=True)
    pending = [parser.parse()]
    fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    edits = []
    while pending:
        node = pending.pop()
        if node[0] == "call":
            pending.extend(node[2])
        elif node[0] in ("binary", "range"):
            pending.extend(node[2:])
        elif node[0] == "unary":
            pending.append(node[2])
        elif node[0] == "ref":
            name, _, _ = split_ref(node[1])
            replacement_name = None
            if name is not None:
                if name == target_id:
                    replacement_name = new_id  # Rename preserves identity; copy creates one.
                elif name.translate(fold) == old_name.translate(fold):
                    replacement_name = new_name
            if replacement_name is not None:
                _, cell = node[1].rsplit("!", 1)
                begin, end = parser.spans[id(node)]
                edits.append((begin+1, end+1, "'"+replacement_name.replace("'", "''")+"'!"+cell))
    out, pos = [], 0
    for begin, end, replacement in sorted(edits):
        out.extend((source[pos:begin], replacement))
        pos = end
    out.append(source[pos:])
    return "".join(out)


def delete_sheet_references(source, local_sheet, target_sheet, *, target_id=None):
    """Make deleted-sheet references permanent errors, including inherited ranges."""
    parser = Parser(source[1:], track_spans=True)
    pending = [parser.parse()]
    fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    def targets(name):
        return (target_id is not None and name == target_id or
                (local_sheet if name is None else name).translate(fold) == target_sheet.translate(fold))
    edits = []
    while pending:
        node = pending.pop()
        if node[0] == "call":
            pending.extend(node[2])
        elif node[0] == "binary":
            pending.extend(node[2:])
        elif node[0] == "unary":
            pending.append(node[2])
        elif node[0] in ("ref","range"):
            if node[0] == "range":
                first, last = node[2:]
                if first[0] != "ref" or last[0] != "ref":
                    raise FormulaError("#REF!", "Range endpoints must be cell references")
                name, _, _ = split_ref(first[1])
                last_name, _, _ = split_ref(last[1])
                if last_name is None:
                    last_name = name
                if targets(name) != targets(last_name):
                    raise FormulaError("#REF!", "Cannot transform a range spanning different sheets")
            else:
                name, _, _ = split_ref(node[1])
            if targets(name):
                begin, end = parser.spans[id(node)]
                edits.append((begin+1,end+1,"#REF!"))
    out, pos = [], 0
    for begin, end, replacement in sorted(edits):
        out.extend((source[pos:begin],replacement))
        pos = end
    out.append(source[pos:])
    return "".join(out), len(edits)


def transform_structure(source, local_sheet, target_sheet, axis, start, count, delete=False, *, target_id=None):
    """Retarget A1 references after a whole-row/column edit; preserve token spelling.

    Absolute markers control copying, not structural edits. A range contracts
    around deleted members and expands for insertions inside its endpoints.
    Return the source and number of newly broken reference expressions.
    """
    parser = Parser(source[1:], track_spans=True)
    pending = [parser.parse()]  # Use the same range semantics as the calculator.
    fold = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    def targets(name):
        if target_id is not None and name == target_id:
            return True  # The calculator also accepts an exact sheet UUID.
        return (local_sheet if name is None else name).translate(fold) == target_sheet.translate(fold)
    def coordinate(ref):
        name, row, col = split_ref(ref)
        return name, row if axis == "row" else col
    maximum = MAX_ROWS if axis == "row" else MAX_COLS
    end = start + count - 1
    def interval(first, last):
        low, high = sorted((first, last))
        if delete:
            if start <= low and high <= end:
                return None
            low = low if low < start else max(low, end+1)-count
            high = high-count if high > end else min(high, start-1)
        else:
            low += count if low >= start else 0
            high += count if high >= start else 0
        if not (1 <= low <= high <= maximum):
            return None
        return (low, high) if first <= last else (high, low)
    def render(ref, value):
        prefix, cell = ref.rsplit("!", 1) if "!" in ref else (None, ref)
        parts = ADDRESS.fullmatch(cell)
        row, col = coordinates(cell)
        if axis == "row": row = value
        else: col = value
        result = f"{parts[1]}{column_name(col)}{parts[3]}{row}"
        return prefix+"!"+result if prefix is not None else result
    edits, broken = [], 0
    while pending:
        node = pending.pop()
        if node[0] == "call":
            pending.extend(reversed(node[2]))
            continue
        if node[0] == "binary":
            pending.extend((node[3],node[2]))
            continue
        if node[0] == "unary":
            pending.append(node[2])
            continue
        if node[0] not in ("ref", "range"):
            continue
        first = last = node
        if node[0] == "range":
            first, last = node[2:]
            if first[0] != "ref" or last[0] != "ref":
                raise FormulaError("#REF!", "Range endpoints must be cell references")
        name, value = coordinate(first[1])
        last_name, last_value = coordinate(last[1])
        if last is not first and last_name is None:
            last_name = name  # Sheet!A1:A3 inherits the first endpoint's sheet.
        if last is not first and targets(name) != targets(last_name):
            raise FormulaError("#REF!", "Cannot transform a range spanning different sheets")
        if targets(name):
            values = interval(value, last_value)
            if values is None:
                begin, finish = parser.spans[id(node)]
                edits.append((begin+1, finish+1, "#REF!"))
                broken += 1
            else:
                endpoints = ((first,value,values[0]),) if last is first else ((first,value,values[0]),(last,last_value,values[1]))
                for endpoint, old, new in endpoints:
                    if old != new:
                        begin, finish = parser.spans[id(endpoint)]
                        edits.append((begin+1,finish+1,render(endpoint[1],new)))
    out, pos = [], 0
    for begin, end, replacement in sorted(edits):
        out.extend((source[pos:begin], replacement))
        pos = end
    out.append(source[pos:])
    return "".join(out), broken


def move_references(source, local_sheet, target_sheet, rectangle, rows, cols, *, target_id=None):
    """Same-sheet cut: referenced targets follow the moved rectangle, even $ refs.

    Discontiguous partial range moves cannot be represented in the scalar tier.
    Moved formulas keep their original external targets, unlike Copy/Fill.
    """
    parser=Parser(source[1:],track_spans=True);pending=[parser.parse()];edits=[]
    fold=str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz")
    r1,c1,r2,c2=rectangle
    def targets(name):
        return (target_id is not None and name==target_id or
                (local_sheet if name is None else name).translate(fold)==target_sheet.translate(fold))
    def render(ref):
        prefix,cell=ref.rsplit("!",1) if "!" in ref else (None,ref)
        parts=ADDRESS.fullmatch(cell);row,col=coordinates(cell)
        result=f"{parts[1]}{column_name(col+cols)}{parts[3]}{row+rows}"
        return prefix+"!"+result if prefix is not None else result
    while pending:
        node=pending.pop()
        if node[0]=="call":pending.extend(node[2]);continue
        if node[0]=="binary":pending.extend(node[2:]);continue
        if node[0]=="unary":pending.append(node[2]);continue
        if node[0] not in ("ref","range"):continue
        first=last=node
        if node[0]=="range":
            first,last=node[2:]
            if first[0]!="ref" or last[0]!="ref":raise FormulaError("#REF!","Move requires cell reference range endpoints")
        name,row,col=split_ref(first[1]);last_name,end_row,end_col=split_ref(last[1])
        if last is not first and last_name is None:last_name=name
        if targets(name)!=targets(last_name):raise FormulaError("#REF!","Cannot move a range spanning different sheets")
        if not targets(name):continue
        low_row,high_row=sorted((row,end_row));low_col,high_col=sorted((col,end_col))
        if high_row<r1 or low_row>r2 or high_col<c1 or low_col>c2:continue
        if not (r1<=low_row<=high_row<=r2 and c1<=low_col<=high_col<=c2):
            raise FormulaError("#REF!","Cut partially intersects a formula range; move its entire referenced range or use Copy")
        for endpoint in (first,) if last is first else (first,last):
            begin,end=parser.spans[id(endpoint)];edits.append((begin+1,end+1,render(endpoint[1])))
    out=[];pos=0
    for begin,end,replacement in sorted(edits):out.extend((source[pos:begin],replacement));pos=end
    out.append(source[pos:]);return "".join(out)


class Parser:
    def __init__(self, source, *, track_spans=False):
        self.tokens, pos = [], 0
        # Optional reference/range offsets use node identity so repeated A1
        # occurrences stay distinct without changing the calculator's AST.
        self.spans = {} if track_spans else None
        self.token_spans = [] if track_spans else None
        offset = len(source)-len(source.lstrip()) if track_spans else 0
        source = source.strip()
        while pos < len(source):
            match = TOKEN.match(source, pos)
            if not match:
                raise FormulaError("#VALUE!", f"Unsupported syntax near {source[pos:pos+20]}")
            self.tokens.append((match.lastgroup, match[match.lastgroup]))
            if track_spans:
                self.token_spans.append((offset+match.start(match.lastgroup),offset+match.end(match.lastgroup)))
            pos = match.end()
            if len(self.tokens) > 1024:
                raise FormulaError("#LIMIT!", "Formula has too many tokens")
        self.tokens.append(("end", ""))
        self.pos = 0

    def take(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def require(self, value):
        if self.take()[1] != value:
            raise FormulaError("#VALUE!", f"Expected {value}")

    def expression(self, minimum=0, depth=0):
        if depth > 128:
            raise FormulaError("#LIMIT!", "Formula nesting limit exceeded")
        start = self.pos
        kind, value = self.take()
        if kind == "number":
            node = ("literal", float(value))
        elif kind == "string":
            node = ("literal", value[1:-1].replace('""', '"'))
        elif kind == "ref":
            node = ("ref", value)
            if self.spans is not None:
                self.spans[id(node)] = self.token_spans[start]
        elif kind == "error":
            node = ("error", value)
        elif value in ("+", "-"):
            node = ("unary", value, self.expression(30, depth + 1))
        elif value == "(":
            node = self.expression(depth=depth + 1)
            self.require(")")
        elif kind == "name":
            if value.upper() in ("TRUE", "FALSE") and self.tokens[self.pos][1] != "(":
                node = ("literal", value.upper() == "TRUE")
            else:
                self.require("(")
                args = []
                if self.tokens[self.pos][1] != ")":
                    while True:
                        args.append(self.expression(depth=depth + 1))
                        if self.tokens[self.pos][1] != ",":
                            break
                        self.take()
                self.require(")")
                node = ("call", value.upper(), args)
        else:
            raise FormulaError("#VALUE!", "Incomplete formula")
        precedence = {"=": 5, "<>": 5, "<": 5, ">": 5, "<=": 5, ">=": 5,
                      "+": 10, "-": 10, "*": 20, "/": 20, "^": 40, ":": 50}
        while self.tokens[self.pos][1] in precedence:
            op = self.tokens[self.pos][1]
            power = precedence[op]
            if power < minimum:
                break
            self.take()
            right = self.expression(power if op == "^" else power + 1, depth + 1)
            node = ("range" if op == ":" else "binary", op, node, right)
            if self.spans is not None and op == ":":
                # Include endpoint parentheses when replacing a whole range,
                # but retain each leaf reference's own span for ordinary edits.
                self.spans[id(node)] = (self.token_spans[start][0],self.token_spans[self.pos-1][1])
        return node

    def parse(self):
        node = self.expression()
        if self.tokens[self.pos][0] != "end":
            raise FormulaError("#VALUE!", "Unexpected trailing input")
        return node


def numeric(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    raise FormulaError("#VALUE!", "Arithmetic requires numeric inputs")


class Calculator:
    def __init__(self, read, resolve_sheet, seconds=0.25):
        self.read, self.resolve_sheet = read, resolve_sheet
        self.cache, self.active = {}, set()
        self.deadline, self.count = time.monotonic() + seconds, 0
        self.bytes = 0

    def check(self):
        self.count += 1
        if self.count > 10_000 or time.monotonic() > self.deadline:
            raise FormulaError("#LIMIT!", "Calculation budget exceeded")

    def cell(self, sheet, row, col):
        self.check()
        key = sheet, row, col
        if key in self.active:
            raise FormulaError("#CYCLE!", "Circular reference")
        if len(self.active) >= 128:
            raise FormulaError("#LIMIT!", "Dependency chain exceeds 128 cells")
        if key in self.cache:
            result = self.cache[key]
            if isinstance(result, FormulaError):
                raise result
            return result
        self.active.add(key)
        try:
            item = self.read(sheet, row, col)
            if item:
                self.bytes += len(item["source"].encode())
                if self.bytes > 8*1024*1024:
                    raise FormulaError("#LIMIT!", "Calculation input cache exceeds 8 MiB")
            if item is None:
                result = None
            elif item["kind"] == "formula":
                result = self.evaluate(Parser(item["source"][1:]).parse(), sheet)
                if isinstance(result, list):
                    raise FormulaError("#VALUE!", "Range cannot be a scalar result")
            else:
                result = item["value"]
            if isinstance(result, (int, float)) and not math.isfinite(result):
                raise FormulaError("#NUM!", "Non-finite result")
            self.cache[key] = result
            return result
        except OverflowError:
            error = FormulaError("#NUM!", "Numeric overflow")
            self.cache[key] = error
            raise error
        except RecursionError:
            error = FormulaError("#LIMIT!", "Calculation nesting limit exceeded")
            self.cache[key] = error
            raise error
        except FormulaError as error:
            self.cache[key] = error
            raise
        finally:
            self.active.remove(key)

    def reference(self, ref, sheet):
        name, row, col = split_ref(ref)
        return self.resolve_sheet(name) if name is not None else sheet, row, col

    def range_targets(self,node,sheet):
        if node[2][0] != "ref" or node[3][0] != "ref":
            raise FormulaError("#REF!", "Range endpoints must be cell references")
        first=self.reference(node[2][1],sheet)
        last_name,_,_=split_ref(node[3][1])
        last=self.reference(node[3][1],first[0] if last_name is None else sheet)
        if first[0]!=last[0]: raise FormulaError("#REF!","3D ranges are unsupported")
        r1,r2=sorted((first[1],last[1])); c1,c2=sorted((first[2],last[2]))
        if (r2-r1+1)*(c2-c1+1)>10_000: raise FormulaError("#LIMIT!","Range exceeds 10,000 cells")
        for row in range(r1,r2+1):
            for col in range(c1,c2+1): yield first[0],row,col

    def argument_values(self,args,sheet):
        """Keep reference provenance and cell errors for range-aware functions."""
        for arg in args:
            if arg[0]=="range":
                try:
                    for target in self.range_targets(arg,sheet):
                        try: value=self.cell(*target)
                        except FormulaError as error:
                            if error.code not in CELL_ERRORS: raise
                            value=error
                        yield value,True
                except FormulaError as error:
                    if error.code not in CELL_ERRORS: raise
                    yield error,True
            else:
                try: value=self.evaluate(arg,sheet)
                except FormulaError as error:
                    if error.code not in CELL_ERRORS: raise
                    value=error
                if isinstance(value,list):
                    for item in value: yield item,True
                else: yield value,arg[0]=="ref"

    @staticmethod
    def logical_value(value):
        if value is None: return False
        if type(value) in (bool,int,float): return bool(value)
        if isinstance(value,str) and value.upper() in ("TRUE","FALSE"): return value.upper()=="TRUE"
        raise FormulaError("#VALUE!","Expected numeric, Boolean or TRUE/FALSE text")

    def logical(self,name,args,sheet):
        if name=="NOT":
            if len(args)!=1: raise FormulaError("#NAME?","NOT requires one scalar argument")
            return not self.logical_value(self.evaluate(args[0],sheet))
        if not 1<=len(args)<=255: raise FormulaError("#NAME?",name+" requires 1-255 arguments")
        result=name=="AND"; count=0
        for value,referenced in self.argument_values(args,sheet):
            self.check()
            if isinstance(value,FormulaError): raise value
            if value is None or (referenced and isinstance(value,str)): continue
            logical=self.logical_value(value); count+=1
            # Evaluate every argument: Python's any/all would hide later errors.
            result=(result and logical) if name=="AND" else (result or logical)
        if not count: raise FormulaError("#VALUE!","Logical aggregate has no numeric or Boolean inputs")
        return result

    def number_function(self,name,args,sheet):
        required=1 if name=="ABS" else 2
        if len(args)!=required: raise FormulaError("#NAME?",name+f" requires {required} arguments")
        number=numeric(self.evaluate(args[0],sheet))
        if name=="ABS": return abs(number)
        digits=numeric(self.evaluate(args[1],sheet))
        if not -15<=digits<=15: raise FormulaError("#NUM!","Rounding supports -15 through 15 decimal places")
        if int(digits)!=digits: raise FormulaError("#VALUE!","Rounding digits must be an integer")
        rounding={"ROUND":ROUND_HALF_UP,"ROUNDUP":ROUND_UP,"ROUNDDOWN":ROUND_DOWN}[name]
        try:
            with localcontext(Context(prec=340)):
                # Round the shortest decimal spelling of the binary64 value.
                result=float(Decimal(str(number)).quantize(Decimal(1).scaleb(-int(digits)),rounding=rounding))
        except (InvalidOperation,OverflowError,ValueError) as error:
            raise FormulaError("#NUM!","Rounding exceeded the numeric range") from error
        if not math.isfinite(result): raise FormulaError("#NUM!","Rounding exceeded the finite range")
        return 0.0 if result==0 else result

    def aggregate(self,name,args,sheet):
        if not 1<=len(args)<=255: raise FormulaError("#NAME?",name+" requires 1-255 arguments")
        count=0; total=0.0; extreme=None
        for value,referenced in self.argument_values(args,sheet):
            self.check()
            if isinstance(value,FormulaError):
                if name=="COUNTA": count+=1; continue
                if name=="COUNT": continue
                raise value
            if name=="COUNTA":
                count+=value is not None
                continue
            if type(value) not in (int,float):
                if referenced or value is None: continue
                if type(value) is bool: value=int(value)
                elif isinstance(value,str) and NUMBER_TEXT.fullmatch(value.strip()): value=float(value.strip())
                elif name=="COUNT": continue
                else: raise FormulaError("#VALUE!",name+" requires numeric direct arguments")
            if not math.isfinite(value):
                if name=="COUNT": continue
                raise FormulaError("#NUM!","Non-finite aggregate input")
            count+=1
            if name=="AVERAGE":
                total+=value
                if not math.isfinite(total): raise FormulaError("#NUM!","AVERAGE subtotal exceeds the finite range")
            elif name in ("MIN","MAX"):
                extreme=value if extreme is None else (min(extreme,value) if name=="MIN" else max(extreme,value))
        if name=="AVERAGE":
            if not count: raise FormulaError("#DIV/0!","AVERAGE has no numeric inputs")
            return total/count
        if name in ("COUNT","COUNTA"): return count
        return extreme if extreme is not None else 0

    def evaluate(self, node, sheet):
        self.check()
        kind = node[0]
        if kind == "literal":
            if isinstance(node[1],(int,float)) and not math.isfinite(node[1]):
                raise FormulaError("#NUM!", "Numeric literal exceeds the finite range")
            return node[1]
        if kind == "error":
            raise FormulaError(node[1])
        if kind == "ref":
            return self.cell(*self.reference(node[1], sheet))
        if kind == "unary":
            value = numeric(self.evaluate(node[2], sheet))
            return -value if node[1] == "-" else value
        if kind == "range":
            return [self.cell(*target) for target in self.range_targets(node,sheet)]
        if kind == "call":
            name, args = node[1:]
            if name=="IFERROR":
                if len(args)!=2: raise FormulaError("#NAME?","IFERROR requires two arguments")
                if args[0][0]=="range": raise FormulaError("#VALUE!","IFERROR requires scalar expressions")
                try: value=self.evaluate(args[0],sheet)
                except FormulaError as error:
                    if error.code not in CELL_ERRORS: raise
                    value=self.evaluate(args[1],sheet)
                if isinstance(value,list): raise FormulaError("#VALUE!","IFERROR array results are unavailable")
                return "" if value is None else value
            if name == "IF" and len(args) in (2, 3):
                condition = self.evaluate(args[0], sheet)
                if not isinstance(condition, (int, float, bool, type(None))):
                    raise FormulaError("#VALUE!", "IF condition must be numeric or boolean")
                return self.evaluate(args[1], sheet) if condition else (
                    self.evaluate(args[2], sheet) if len(args) == 3 else False)
            if name in ("ABS","ROUND","ROUNDUP","ROUNDDOWN"):
                return self.number_function(name,args,sheet)
            if name in ("AND","OR","NOT"):
                return self.logical(name,args,sheet)
            if name in ("AVERAGE","MIN","MAX","COUNT","COUNTA"):
                return self.aggregate(name,args,sheet)
            if name == "SUM":
                total = 0
                for arg in args:
                    value = self.evaluate(arg, sheet)
                    for item in value if isinstance(value, list) else [value]:
                        if isinstance(item, (int, float)) and not isinstance(item, bool):
                            total += item
                            if not math.isfinite(total):
                                raise FormulaError("#NUM!", "SUM exceeds the finite numeric range")
                return total
            raise FormulaError("#NAME?", f"Unsupported function or arity: {name}")
        if kind == "binary":
            op = node[1]
            left, right = self.evaluate(node[2], sheet), self.evaluate(node[3], sheet)
            if isinstance(left, list) or isinstance(right, list):
                raise FormulaError("#VALUE!", "Array expressions are unsupported; use a range-aware function")
            if op in ("=", "<>", "<", ">", "<=", ">="):
                if left is None or right is None:
                    # Blank comparisons permit numeric/Boolean coercion only.
                    left, right = numeric(left), numeric(right)
                if type(left) != type(right) and not (
                        type(left) in (float, int) and type(right) in (float, int)):
                    raise FormulaError("#VALUE!", "Comparison types differ")
                return {"=": lambda: left == right, "<>": lambda: left != right,
                        "<": lambda: left < right, ">": lambda: left > right,
                        "<=": lambda: left <= right, ">=": lambda: left >= right}[op]()
            left, right = numeric(left), numeric(right)
            if op == "/" and right == 0:
                raise FormulaError("#DIV/0!")
            if op == "^" and (abs(right) > 1024 or (left == 0 and right < 0)):
                raise FormulaError("#NUM!", "Exponent exceeds the supported numeric range")
            result = {"+": lambda: left + right, "-": lambda: left - right,
                      "*": lambda: left * right, "/": lambda: left / right,
                      "^": lambda: left ** right}[op]()
            if isinstance(result, complex) or not math.isfinite(result):
                raise FormulaError("#NUM!")
            return result
        raise FormulaError("#VALUE!")
