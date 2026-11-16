# Chart authoring wireframes

Status: Signed off by the owner on November 15, 2026, with the nine decisions and the sixteen owner questions accepted as written.

Wireframes, November 15, 2026. These Qt Widgets sketches define how a person
inserts, selects, moves, resizes, reorders, deletes and edits a chart on the
desktop. They draw sections 4 and 5 of the
[authoring and export design](charts-authoring-export-design.md) and follow the
[authoring plan](charts-authoring-plan.md), whose nine decisions are restated at
the end for confirmation. No desktop code is written until this document is
signed off. Later tasks copy every label, status text and message on this page
verbatim.

The starting point is the mockup the owner chose (option A, "Insert at once,
then adjust"): Insert → Chart puts a suggested chart beside the selection,
selects it and opens the side panel. There is no insert dialog, type picker or
preview. Where the mockup and the design differ, the design wins.

**How to read the sketches.** `[Text]` is a button or an editable field,
`[Text   v]` a drop-down list, `[*Text]` a pressed (checked) button, `{Text}`
a control shown but disabled, and `<<Text>>` the field being picked. `#` marks
the selected chart's frame and `o` its handles. Text in quotation marks in the
prose is shown exactly as written. Sample data: months in A2:A13, units in
B2:B13 and returns in C2:C13 on Sheet1.

## 1. Main window with the Insert menu open

```text
+------------------------------------------------------------------------------+
| File  Edit  Insert  Format  Sheet  Data  Tools  View  Help                   |
|             +-------------------------------------------+                    |
| [New][Open] | Chart                              Alt+F1 |                    |
| [A1      ]  |-------------------------------------------|                    |
|      A      | Select Next Chart              Ctrl+Alt+C |       F        G   |
|  1 Month    | Select Previous Chart    Ctrl+Alt+Shift+C |                    |
|  2 Jan      +-------------------------------------------+                    |
|  3 Feb          55        9                                                  |
|  4 Mar          70       14                                                  |
|  5 Apr          50        6                                                  |
|  ...                                                                         |
| 13 Dec          65       11                                                  |
|                                                                              |
| [Sheet1] [Sheet2]                                                            |
+------------------------------------------------------------------------------+
| (status text as today)                              Storage and calculation  |
+------------------------------------------------------------------------------+
```

The Insert menu is new and sits between Edit and Format, so the menu bar reads
File, Edit, Insert, Format, Sheet, Data, Tools, View, Help. It holds three
commands, in this order, with a separator after the first:

| Command | Shortcut | Object name |
| --- | --- | --- |
| Chart | Alt+F1 | `insertChartAction` |
| Select Next Chart | Ctrl+Alt+C | `selectNextChartAction` |
| Select Previous Chart | Ctrl+Alt+Shift+C | `selectPreviousChartAction` |

Qt shows these shortcuts in the platform's form: on macOS Ctrl is Command and
Alt is Option, so the menu shows ⌥F1, ⌘⌥C and ⇧⌘⌥C.

Chart inserts at once from the grid's one rectangular selection. The chart is
anchored on the selection's top row, one column to the right of it (on the
selection's own top-left cell when that column does not exist or is hidden), at
the engine's default name ("Chart 1", "Chart 2" and so on) and size (480 by 300
pixels). It is selected, the panel opens on it, focus stays on the chart, and
the status bar reads "Chart inserted • Undo removes it". When the service
refuses (no numeric data, more than 10,000 cells, hidden rows in the selection,
the chart limit), its message goes to Issues and nothing is inserted.

Chart is disabled with no workbook open, in a read-only workbook, in a filtered
or sorted list view (charts are hidden there) and while Tables and queries is
shown. Select Next Chart and Select Previous Chart move selection and focus to
the next or previous chart on the current sheet in stacking order, wrapping at
the ends; they are disabled when the sheet shows no charts, in a list view and
while Tables and queries is shown. Selecting is allowed in a read-only workbook;
changing is not.

## 2. A sheet with a selected chart and the panel docked right

```text
+------------------------------------------------------------------------------+
| File  Edit  Insert  Format  Sheet  Data  Tools  View  Help                   |
| [New][Open][Save] ...                                                        |
| [A1      ] [Month                                       ]                    |
+--------------------------------------------------+---------------------------+
|      A       B       C     D        E        F   | Chart                   x |
|  1 Month   Units  Returns                        | +- Chart ----------------+|
|  2 Jan        40       5  o#########o#########o  | | Name [Units by month ] ||
|  3 Feb        55       9  #  Units by month   #  | | [Bring to Front]       ||
|  4 Mar        70      14  #  _                #  | | [Send to Back]         ||
|  5 Apr        50       6  o | |  _   _     .  o  | +------------------------+|
|  6 May        90      12  #  | | | | | | _ |  #  | +- Type -----------------+|
|  7 Jun        65       8  #  |_|_|_|_|_|_|_|  #  | | [*Column] [Bar] [Line] ||
|  8 Jul        60       7  o#########o#########o  | | [Area] [Pie] [Scatter] ||
|  9 Aug        75      10                         | | Grouping [Clustered v] ||
| 10 Sep        80      13                         | +------------------------+|
| ...                                              | ...                       |
| [Sheet1] [Sheet2]                                |                           |
+--------------------------------------------------+---------------------------+
| Chart inserted • Undo removes it                    Storage and calculation  |
+------------------------------------------------------------------------------+
```

The selected chart has a 2 pixel frame in the palette's highlight colour and
eight square handles, 8 by 8 pixels, at the corners and the midpoints of the
edges. Frame and handles are drawn inside the chart's own rectangle, so the
chart never paints over the row or column headers, the formula bar or another
pane. Only one chart is selected at a time.

A click on a chart selects it without opening the panel; a double-click selects
it and opens the panel. A click on any grid pane deselects the chart and
selects the cell as usual. The mouse wheel over a chart scrolls the grid.
Dragging the chart moves it and dragging a handle resizes it (64 by 48 to 4096
by 4096 pixels); the chart follows the pointer and one change is sent on
release. During a resize the size stops at the bounds: it does not shrink
below 64 by 48 or grow past 4096 by 4096. A drag does not scroll the grid. On
release one update is sent with the anchor cell under the chart's top-left
corner, in whichever pane (frozen corner, frozen rows, frozen columns or body)
is there, and the offset of that corner inside that cell; when no cell is there (over a header,
outside the grid, past the last row or column) the drag is cancelled and the
chart goes back. A release that moved nothing sends nothing.

The panel is a dock on the right titled "Chart" (`chartDock`), in the manner of
the Issues dock at the bottom. It is hidden when the application starts. It
opens when a chart is inserted, on Edit Chart, and on a double-click or Enter on
a chart. Once open it stays open while the selection changes and shows the
selected chart. View → Chart shows and hides it.

## 3. The panel in full

A column chart with two series, as Insert → Chart makes it from A1:C13.

```text
+- Chart -------------------------------------------- x -+
| +- Chart --------------------------------------------+ |
| | Name  [Units by month                            ] | |
| | [Bring to Front]  [Send to Back]                   | |
| +----------------------------------------------------+ |
| +- Type ---------------------------------------------+ |
| | [*Column]  [Bar]  [Line]  [Area]  [Pie]  [Scatter] | |
| | Grouping  [Clustered                            v] | |
| +----------------------------------------------------+ |
| +- Title --------------------------------------------+ |
| | [Units by month                                  ] | |
| +----------------------------------------------------+ |
| +- Data ---------------------------------------------+ |
| | Categories [Range       v] [A2:A13        ] [Pick] | |
| | Series                                             | |
| | +- Series 1 -------------------------------------+ | |
| | | Name   [Cell        v] [B1             ] [Pick]| | |
| | | Values [Range       v] [B2:B13         ] [Pick]| | |
| | | [Remove]  [Move Up]  {Move Down}               | | |
| | +------------------------------------------------+ | |
| | +- Series 2 -------------------------------------+ | |
| | | Name   [Cell        v] [C1             ] [Pick]| | |
| | | Values [Range       v] [C2:C13         ] [Pick]| | |
| | | [Remove]  {Move Up}  [Move Down]               | | |
| | +------------------------------------------------+ | |
| | [Add Series]                                       | |
| +----------------------------------------------------+ |
| +- Axes ---------------------------------------------+ |
| | Category title [                                 ] | |
| | Value title    [Units                            ] | |
| | Minimum        [Automatic                        ] | |
| | Maximum        [Automatic                        ] | |
| +----------------------------------------------------+ |
| +- Legend -------------------------------------------+ |
| | [Right                                          v] | |
| +----------------------------------------------------+ |
+--------------------------------------------------------+
```

**Groups.** Six groups in this order: Chart, Type, Title, Data, Axes, Legend.
The panel scrolls vertically when the dock is shorter than its content; it
never scrolls sideways.

- **Chart.** Name (1 to 64 characters). Bring to Front and Send to Back change
  the stacking order of the chart among the sheet's charts.
- **Type.** Six buttons, Column, Bar, Line, Area, Pie and Scatter, exactly one
  pressed. Grouping offers Clustered and Stacked; it is enabled for Column, Bar
  and Area and disabled and empty for Line, Pie and Scatter. A type change is
  one change that also carries what the engine requires and loses no source:
  grouping becomes Clustered when the new type takes one and the old did not,
  and none when it takes none; to Scatter the categories become every series'
  X; from Scatter the first series' X becomes the categories. A chart with no
  categories cannot become a Scatter until an X source is given: the change is
  refused, the type goes back, and the message beside the type buttons says so
  ("Give an X source before changing this chart to a scatter chart.").
- **Title.** One field for the chart title, at most 255 characters; empty means
  no title.
- **Data.** Categories, then the Series list. Each series has Name, Values and,
  when the type is Scatter, X (section 4). For a Scatter chart the Categories
  row is not shown, because its role is taken by each series' X. Each series has
  Remove, Move Up and Move Down; Move Up is disabled on the first series and
  Move Down on the last. Add Series adds an empty series block to the panel only;
  nothing is sent until its Values (and X, for Scatter) are filled in, because
  the engine has no series without values. Removing the last series is refused
  in the panel with "A chart needs at least one series".
- **Axes.** Category title and Value title (each at most 255 characters; empty
  means none), Minimum and Maximum. Minimum and Maximum take a number or
  nothing; an empty field shows the placeholder "Automatic" and means the
  engine chooses.
- **Legend.** One list: Right, Bottom, None.

**Applying.** There is no Apply button. A choice (a type button, Grouping,
Legend, Bring to Front, Send to Back, Remove, Move Up, Move Down, a source kind
or list) applies when it changes. A text field applies on Enter or when it
loses focus, and only when its text differs from the chart's stored value.
Every change is one operation and one Undo. While a change is in flight the
panel sends no second one.

**Qt mapping.** `ChartPanel` (`chartPanel`) inside a `QScrollArea` in the
`QDockWidget` `chartDock`. Each group is a `QGroupBox` titled as above, laid out
with `QFormLayout`. Name, Title, the axis titles, Minimum and Maximum are
`QLineEdit`s (`chartName`, `chartTitle`, `categoryAxisTitle`,
`valueAxisTitle`, `valueAxisMin`, `valueAxisMax`); Minimum and Maximum use the
placeholder text "Automatic". The type buttons are checkable `QToolButton`s
(`chartType_column` … `chartType_scatter`) in an exclusive `QButtonGroup`.
Grouping (`chartGrouping`) and Legend (`legendPosition`) are `QComboBox`es.
Bring to Front and Send to Back are `QPushButton`s (`chartToFront`,
`chartToBack`). Each series is a `QGroupBox` titled "Series <n>" holding its
source fields and the buttons `removeSeries<n>`, `seriesUp<n>`,
`seriesDown<n>`; Add Series is `addSeries`. Below every field that can be
refused sits a hidden word-wrapped `QLabel` named `<field>Message` (section 7).

## 4. Source fields in each of their three kinds

```text
Range
| Values [Range        v] [B2:B13                     ] [Pick] |

Table column
| Values [Table column v] [Orders          v] [Quantity     v] |

Query column
| Values [Query column v] [Customer totals v] [Total        v] |

A series name, as text and as a cell
| Name   [Text         v] [Units                             ] |
| Name   [Cell         v] [B1                         ] [Pick] |

A Scatter series
| +- Series 1 --------------------------------------+ |
| | Name   [Cell        v] [B1             ] [Pick] | |
| | Values [Range       v] [B2:B13         ] [Pick] | |
| | X      [Range       v] [A2:A13         ] [Pick] | |
| | [Remove]  {Move Up}  {Move Down}                | |
| +-------------------------------------------------+ |
```

Categories, Values and X are source fields. Each starts with a kind list:
Range, Table column, Query column.

- **Range.** A text field and a Pick button. The field shows the reference
  without a sheet name when the range is on the chart's own sheet
  (`B2:B13`) and with it otherwise (`Sheet2!B2:B13`); either form may be typed.
  Pick starts a range pick on the grid (section 5).
- **Table column.** Two lists: the workbook's tables, then the chosen table's
  columns. Tables are held by identity, so renaming a table or column later
  needs no change to the chart.
- **Query column.** Two lists: the workbook's saved queries, then the chosen
  query's result columns. A saved query whose parameters have no saved
  defaults is refused by the service with its reason beside the field.

A series Name has its own kind list with two entries only, Text and Cell,
because the engine takes a fixed text or a single cell. Text shows a text field
(at most 255 characters); Cell shows a text field and a Pick button and accepts
one cell only. Changing a kind clears the field it shows; nothing is sent until
the new source is complete.

A typed range on an unknown sheet, or text that is not a reference, is refused
in the panel beside the field without a request. A source over a size limit is
refused by the service with its message beside the field.

**Qt mapping.** A `ChartSourceField` widget: a `QComboBox` for the kind, then a
`QLineEdit` and a `QToolButton` "Pick" for Range, or two `QComboBox`es for
Table column and Query column, in a `QStackedLayout`. Object names:
`categoriesSource`, `series<n>Name`, `series<n>Values`, `series<n>X`. The Pick
button's accessible name is "Pick <field> on the grid", for example "Pick
Series 1 Values on the grid". The lists of tables and saved queries are read
again after every workbook refresh while the panel is shown.

## 5. The panel during a range pick

```text
+------------------------------------------------+---------------------------+
|      A       B       C     D        E        F | Chart                   x |
|  1 Month   Units  Returns                      | ...                       |
|  2 Jan     +-----+     5  o#########o#######o  | +- Data -----------------+|
|  3 Feb     |  55 |     9  #  Units by month #  | | Categories [Range  v]  ||
|  4 Mar     |  70 |    14  #                 #  | |   [A2:A13     ] [Pick] ||
|  5 Apr     |  50 |     6  o                 o  | | +- Series 1 ---------+ ||
|  6 May     |  90 |    12  #                 #  | | | Values [Range  v]  | ||
|  7 Jun     +-----+     8  o#########o#######o  | | | <<B3:B6>>  [Pick]  | ||
|  8 Jul        60       7                       | | +--------------------+ ||
| ...                                            | ...                       |
| [Sheet1] [Sheet2]                              |                           |
+------------------------------------------------+---------------------------+
| Pick a range for Series 1 Values • Enter applies • Escape cancels          |
+----------------------------------------------------------------------------+
```

Pick on a Range or Cell field starts the pick. The field is highlighted with the
palette's highlight colour and its Pick button stays pressed. The status bar
reads "Pick a range for <field> • Enter applies • Escape cancels", where
<field> is the field's label: "Categories", "Series <n> Name",
"Series <n> Values" or "Series <n> X".

While the pick is active, selecting cells on any sheet fills the field as the
selection changes, and the chart stays selected: this is the one case where a
click on the grid does not deselect it. The user may switch to another sheet;
the field then shows the sheet name. Commands that edit cells are disabled, as
during the existing Python reference pick. Enter applies the picked range as one
change; Escape cancels and puts the field back. Both return to the chart's
sheet, select the chart again and put focus in the field. For a Name field the
pick takes one cell; while more than one is selected the status reads "Pick one
cell for Series <n> Name • Enter applies • Escape cancels" and Enter does
nothing. Starting a cell edit, closing the workbook or switching to a list view
cancels the pick.

## 6. The context menu on a chart

```text
      o#########o#########o
      #  Units by month   #
      #  _                #
      o | |  _  +------------------+
      #  | | | | Edit Chart        |
      #  |_|_|_| Bring to Front    |
      o#########o| Send to Back      |
                 |-------------------|
                 | Delete            |
                 +-------------------+
```

A right-click on a chart selects it and opens this menu. The menu key or
Shift+F10 opens it on the selected chart. Edit Chart (`editChartAction`) opens
the panel on the chart and moves focus to it. Bring to Front
(`chartToFrontAction`) and Send to Back (`chartToBackAction`) change the
stacking order. Delete (`deleteChartAction`) removes the chart without a
confirmation; Undo restores it. In a read-only workbook or while busy only Edit
Chart is enabled, and the panel opens locked.

## 7. States

### No chart selected

```text
+- Chart ------------------------------- x -+
|                                           |
|            No chart selected              |
|                                           |
+-------------------------------------------+
```

The panel shows only the centred text "No chart selected" when no chart is
selected: when the user clicks the grid or presses Escape, after a sheet switch,
and when the selected chart stops existing (Undo of its creation, a delete from
another client). A text field that had focus and unsent text sends nothing when
the chart goes away.

### A refused change

```text
| +- Type ---------------------------------------------+ |
| | [*Column]  [Bar]  [Line]  [Area]  [Pie]  [Scatter] | |
| | ! A pie chart takes exactly one series             | |
| | Grouping  [Clustered                            v] | |
| +----------------------------------------------------+ |
```

The chart is unchanged, the field (here the type buttons) is put back to the
chart's stored value, and the service's own message appears directly beneath
the field in the palette's error text colour. The message shown above is only
an example; the text is always the service's. The message stays until the
chart is reloaded or that field is edited again, and is also set as the field's
accessible description. As today for every refused operation, the full
message is also reported in the Issues dock by `MainWindow::submit`; this is
existing behaviour, not new to charts. A change
refused in the panel itself (an unknown sheet, a bad reference, removing the
last series) is shown the same way. The panel never drops data to make a change
fit: a two-series chart is not cut to one to become a pie.

### A broken source

```text
+- Chart -------------------------------------------- x -+
| ! A source of this chart no longer exists              |
| +- Chart --------------------------------------------+ |
| | Name  [Units by month                            ] | |
| | [Bring to Front]  [Send to Back]                   | |
| +----------------------------------------------------+ |
| +- Type ---------------------------------------------+ |
| | {*Column} {Bar} {Line} {Area} {Pie} {Scatter}      | |
| | Grouping  {Clustered                            v} | |
| +----------------------------------------------------+ |
| +- Title --------------------------------------------+ |
| | {Units by month                                  } | |
| +----------------------------------------------------+ |
| +- Data ---------------------------------------------+ |
| | Categories [Range       v] [Old!A2:A13    ] [Pick] | |
| |            ! This source no longer exists          | |
| | +- Series 1 -------------------------------------+ | |
| | | Name   [Cell        v] [B1             ] [Pick]| | |
| | | Values [Range       v] [B2:B13         ] [Pick]| | |
| | |        Not applied yet                         | | |
| | +------------------------------------------------+ | |
| | +- Series 2 -------------------------------------+ | |
| | | Name   [Cell        v] [Old!C1         ] [Pick]| | |
| | |        ! This source no longer exists          | | |
| | | Values [Range       v] [Old!C2:C13     ] [Pick]| | |
| | |        ! This source no longer exists          | | |
| | +------------------------------------------------+ | |
| | ...                                                | |
| +----------------------------------------------------+ |
| (Axes and Legend disabled)                             |
+--------------------------------------------------------+
```

The reason heads the panel. The Chart group stays enabled (rename, Bring to
Front, Send to Back), as do the source fields and the Data group's buttons.
Type, Title, Axes and Legend are disabled, because the engine cannot accept a
specification that holds a broken source. Each broken field the user has not
yet replaced says "This source no longer exists". Its old reference or name is
not shown, because the engine clears it when the source breaks: the field is
blank (the sketch's `Old!A2:A13` cannot be shown). A source field the user has
edited is held in the panel and marked "Not applied yet" instead (in the sketch,
Series 1 Values was edited; Categories and both Series 2 fields are still
broken). Series added, removed or moved are held the same way. When no
broken source remains, the held changes are sent together as one change and
the reason goes away. On the sheet the chart shows its placeholder with the same
reason and can still be selected, moved, resized, reordered and deleted.

### An unsupported or invalid specification

```text
+- Chart -------------------------------------------- x -+
| ! This chart cannot be read (UNSUPPORTED_SPEC_VERSION) |
| +- Chart --------------------------------------------+ |
| | Name  [Units by month                            ] | |
| | [Bring to Front]  [Send to Back]                   | |
| +----------------------------------------------------+ |
| +- Type ---------------------------------------------+ |
| | {Column} {Bar} {Line} {Area} {Pie} {Scatter}       | |
| | Grouping  {                                     v} | |
| +----------------------------------------------------+ |
| (Title, Data, Axes and Legend disabled and empty)      |
+--------------------------------------------------------+
```

A chart whose specification is of a newer version, or cannot be read, shows the
reason at the top ("This chart cannot be read (UNSUPPORTED_SPEC_VERSION)" or
"This chart cannot be read (INVALID_SPEC)"). Only the Chart group is enabled;
every other group is disabled and empty, because the panel cannot show a
specification it cannot read and never rewrites one. The chart can still be
selected, moved, resized, reordered, renamed and deleted, because none of those
sends a specification: a rename sends only the name, a move or resize only the
placement, a reorder only the order, and a delete only the chart. A
specification is sent only when a specification field (Type, Title, Data, Axes
or Legend) changes. The same holds for a chart with a broken source.

A chart whose sources are over a size limit is readable: the panel shows the
reason at the top ("This chart's sources are over a size limit (…)") and every
group stays enabled so the sources can be made smaller.

### A chart still drawing

```text
      o#########o#########o
      #                   #
      #  Units by month   #
      o  Loading          o
      #                   #
      o#########o#########o
```

Until the service returns the first image, the chart shows the same-size
placeholder the view-only layer shows today, with the chart's name and
"Loading". After a change the previous image stays, scaled if the size changed,
until the new one arrives. The chart can be selected, moved, resized and edited
while it draws; the panel works normally and shows no reason.

### A stale chart

A chart whose picture comes from an older workbook state, and is being drawn
again, looks and behaves as the view-only layer shows it today: the older
picture stays in place until the new one arrives. The chart can still be
selected, moved, resized, deleted and opened in the panel, and the panel works
normally.

### Busy

```text
+- Chart -------------------------------------------- x -+
| +- Chart --------------------------------------------+ |
| | Name  {Units by month                            } | |
| | {Bring to Front}  {Send to Back}                   | |
| +----------------------------------------------------+ |
| +- Type ---------------------------------------------+ |
| | {*Column} {Bar} {Line} {Area} {Pie} {Scatter}      | |
| ...  (every control disabled; values kept)             |
+--------------------------------------------------------+
```

While any operation is in flight, the panel keeps showing its values with every
control disabled, and charts can be selected but not dragged, resized or
changed from the keyboard. Key auto-repeat is ignored. The status bar shows the
existing busy text for the operation. When the operation finishes, the catalog
is read again and the panel reloads from it. A read-only workbook shows the
panel the same way, locked.

## Keyboard

| Where | Key | Result |
| --- | --- | --- |
| A sheet grid (not a list view or Tables and queries) | Alt+F1 | Insert a chart from the selection |
| A sheet grid (not a list view or Tables and queries) | Ctrl+Alt+C | Select the next chart and focus it |
| A sheet grid (not a list view or Tables and queries) | Ctrl+Alt+Shift+C | Select the previous chart and focus it |
| Selected chart | Arrow | Move 8 pixels |
| Selected chart | Alt+Arrow | Move 1 pixel |
| Selected chart | Shift+Arrow | Resize by 8 pixels (right and bottom edges) |
| Selected chart | Shift+Alt+Arrow | Resize by 1 pixel |
| Selected chart | Delete or Backspace | Delete the chart (never the cells beneath) |
| Selected chart | Enter or Return | Open the panel and move focus to it |
| Selected chart | Menu key or Shift+F10 | Open the context menu |
| Selected chart | Escape | Deselect and return focus to the grid |
| During a drag | Escape | Cancel the drag; the chart goes back and stays selected |
| During a range pick | Enter | Apply the pick, reselect the chart, focus the field |
| During a range pick | Escape | Cancel the pick, reselect the chart, focus the field |
| Panel text field | Enter | Apply the text if it changed |
| Panel | Tab, Shift+Tab | Move through the panel in the focus order below |

Each arrow-key move or resize is one change and one Undo. A keyboard move that
would put the chart past an offset of 4096 pixels or onto no cell is refused in
the window with the status "The chart cannot move further". A keyboard resize
that would take the size below 64 by 48 or past 4096 by 4096 is refused the same
way with the status "The chart cannot resize further"; the size stays at the
bound. The three Insert commands follow the enablement in section 1: none works
with no workbook, in a list view or while Tables and queries is shown, Chart is
also disabled in a read-only workbook, and the Select commands are disabled when
the sheet shows no charts. A selected chart
takes Delete, Backspace, Escape, Enter, Return and the arrows before the
window's own shortcuts, so Delete never clears the cells under the grid cursor.
The macOS forms of these keys follow Qt's mapping (Ctrl is Command, Alt is
Option).

## Focus order through the panel

1. Name
2. Bring to Front, Send to Back
3. Column, Bar, Line, Area, Pie, Scatter
4. Grouping
5. Title
6. Categories: kind, then its field and Pick (or its two lists)
7. For each series in order: Name kind, Name field, Pick; Values kind, field,
   Pick (or lists); X kind, field, Pick (or lists) for Scatter; Remove, Move
   Up, Move Down
8. Add Series
9. Category title, Value title, Minimum, Maximum
10. Legend

Disabled controls are skipped. A refusal message is not a focus stop; it is the
field's accessible description. After Enter on a chart, or Edit Chart, focus
lands on Name. After a range pick, focus returns to the picked field.

## Accessible names and descriptions

Each chart on the sheet is a focusable control with the accessible name
"Chart: <name>", for example "Chart: Units by month", and the description
"<Type> chart, titled <title>, <n> series", for example "Column chart, titled
Units by month, 2 series". When the chart has no title the middle part is left
out: "Column chart, 2 series". The count reads "1 series" for one series and
"<n> series" otherwise. A chart that cannot be read or drawn has the
reason as its description instead.

Every panel control has an accessible name equal to its visible label; fields
without a visible label use their group's name ("Title", "Legend"), series
fields include the series ("Series 1 Values"), and the Pick buttons are named
"Pick <field> on the grid". The reason at the top of the panel
(`chartPanelReason`) is a label read with the panel.

## Status texts and messages

Status-bar texts (short phrases joined with " • "):

| Text | When |
| --- | --- |
| "Chart inserted • Undo removes it" | After Insert → Chart succeeds |
| "Pick a range for <field> • Enter applies • Escape cancels" | During a range pick |
| "Pick one cell for Series <n> Name • Enter applies • Escape cancels" | A Name pick with more than one cell selected |
| "The chart cannot move further" | A keyboard move past the limits or onto no cell |
| "The chart cannot resize further" | A keyboard resize past the size bounds |

Panel texts and field markers:

| Text | Where |
| --- | --- |
| "No chart selected" | The whole panel, with nothing selected |
| "A source of this chart no longer exists" | Panel reason at the top: a broken source |
| "This chart cannot be read (<code>)" | Panel reason at the top: unsupported or invalid specification |
| "This chart's sources are over a size limit (<limit>)" | Panel reason at the top: over a limit |
| "This source no longer exists" | Beneath a broken source field not yet edited |
| "Not applied yet" | Beneath a source field edited while a source is broken |
| "A chart needs at least one series" | Beneath the series list: Remove on the only series (refused in the panel) |
| "Give an X source before changing this chart to a scatter chart." | Beneath the type buttons: Scatter on a chart with no categories |
| The service's own message | Beneath the refused field, for every refusal from the service |
| "Enter a number or leave empty for automatic" | Beneath Minimum or Maximum: the text is not a number (refused in the panel) |
| "Enter a cell range such as A2:A13" | Beneath a Range or Cell field: an unknown sheet or text that is not a reference (refused in the panel) |
| "A series name takes one cell" | Beneath a series Name set to Cell: more than one cell typed (refused in the panel) |
| "Not applied yet" | Beneath the Values (and, for Scatter, X) of a series added with Add Series, until its values are given and it is sent |

The last four rows were added during the build, November 2026: not part of the original sign-off.

## Decisions for the owner to confirm

1. **Every panel change sends the whole chart.** Each change from the panel
   sends the chart's complete specification rather than only the part that
   changed, with tables, columns and saved queries named by identity so a later
   rename cannot redirect it.
2. **A broken source holds back other changes.** While any source of a chart no
   longer exists, only its name, position and stacking order can change at
   once; source edits are kept in the panel, marked "Not applied yet", and sent
   together when nothing is broken any more.
3. **Shortcuts.** Insert Chart is Alt+F1, Select Next Chart Ctrl+Alt+C, Select
   Previous Chart Ctrl+Alt+Shift+C; on a selected chart the arrows move it by 8
   pixels (Alt for 1), Shift with the arrows resizes it by 8 pixels (Shift+Alt
   for 1), Delete or Backspace
   deletes it, Enter opens the panel and Escape returns to the grid.
4. **Handles.** Eight 8-pixel handles at the corners and edge midpoints and a
   2-pixel frame in the highlight colour, drawn inside the chart so nothing
   spills over the headers or other panes.
5. **Dragging does not scroll.** A drag never scrolls the grid; on release the
   chart anchors to the cell under its top-left corner in whatever pane is
   there, and a release over no cell puts the chart back.
6. **Changes apply at once, no delay.** Choices apply when changed and text on
   Enter or leaving the field, only if it differs; while a change is in flight
   the panel and dragging are disabled.
7. **A change that crosses another client's change is not retried.** If the
   workbook changed underneath, the window refreshes as it does today and the
   panel reloads; the user makes the change again if still wanted.
8. **Authoring tests get their own test program**, because the existing desktop
   test program is close to its time limit on the build servers.
9. **A range pick may visit other sheets.** During a pick the user may switch
   sheets; Enter or Escape returns to the chart's sheet and selects the chart
   again.

## Owner questions

These are details the design and plan leave open, filled in for these
wireframes. Each can be accepted or corrected on its own.

1. For a Scatter chart the Categories row is hidden and each series shows an X
   row; for every other type the X row is not shown.
2. A range field shows the bare reference (`B2:B13`) when the range is on the
   chart's own sheet and the sheet-qualified form (`Sheet2!B2:B13`) otherwise,
   and either form may be typed.
3. A broken source field not yet replaced shows "This source no longer exists",
   and adding, removing or moving series is held in the panel under decision 2,
   just as source edits are.
4. A chart whose sources are over a size limit shows the reason at the top of
   the panel and keeps every group enabled, so the sources can be made smaller.
5. Select Next Chart and Select Previous Chart stay enabled in a read-only
   workbook, because selecting changes nothing; they are disabled with no
   charts on the sheet, in a list view and in Tables and queries. This is the
   item closest to the rule that authoring commands are disabled in a
   read-only workbook.
6. In a read-only workbook or while busy the chart's context menu enables only
   Edit Chart, and the panel opens locked.
7. Shift with the arrows resizes from the right and bottom edges, keeping the
   top-left corner where it is.
8. Enter on a chart, or Edit Chart, puts focus on the panel's Name field.
9. A chart that cannot be read or drawn uses its reason as its accessible
   description instead of "<Type> chart, titled <title>, <n> series".
10. While a series name pick has more than one cell selected, the status reads
    "Pick one cell for Series <n> Name • Enter applies • Escape cancels" and
    Enter does nothing.
11. The Title field and the Legend list have no visible label inside their
    groups (their accessible names are "Title" and "Legend"), and the pick
    button reads "Pick", with the accessible name "Pick <field> on the grid".
12. Delete removes a chart without asking for confirmation, because Undo
    restores it.
13. The Menu key or Shift+F10 opens the context menu on the selected chart.
14. A refused change is also reported in the Issues dock, as every refused
    operation already is today.
15. Remove on a chart's only series is refused in the panel with "A chart needs
    at least one series", as plan Task 7 specifies.
16. Alt+F1 may need the fn key on a Mac keyboard, where F1 is a media key by
    default.
