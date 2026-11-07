// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "servicepaths.h"
#include "frozenpanes.h"
#include <QGridLayout>
#include <QApplication>
#include <QClipboard>
#include <QCheckBox>
#include <QCloseEvent>
#include <QColorDialog>
#include <QCryptographicHash>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QDockWidget>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QHeaderView>
#include <QInputDialog>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QKeyEvent>
#include <QLineEdit>
#include <QMenuBar>
#include <QMessageBox>
#include <QMimeData>
#include <QPlainTextEdit>
#include <QPainter>
#include <QPushButton>
#include <QPointer>
#include <QPersistentModelIndex>
#include <QProcessEnvironment>
#include <QProgressDialog>
#include <QRegularExpression>
#include <QScrollBar>
#include <QScopedValueRollback>
#include <QSettings>
#include <QSaveFile>
#include <QStandardPaths>
#include <QSpinBox>
#include <QStyledItemDelegate>
#include <QSignalBlocker>
#include <QStatusBar>
#include <QTabBar>
#include <QTableView>
#include <QTimer>
#include <QToolBar>
#include <QUuid>
#include <QVBoxLayout>
#include <filesystem>
#include <algorithm>

namespace {
bool sameFile(const QString &left, const QString &right) {
    std::error_code error;
    return std::filesystem::equivalent(QFileInfo(left).filesystemFilePath(), QFileInfo(right).filesystemFilePath(), error);
}

class ExportFileDialog : public QFileDialog {
public:
    ExportFileDialog(QWidget *parent, const QStringList &filters) : QFileDialog(parent) {
        setObjectName("exportFileDialog");
        setOption(QFileDialog::DontUseNativeDialog);
        setAcceptMode(QFileDialog::AcceptSave); setFileMode(QFileDialog::AnyFile);
        setNameFilters(filters); setDefaultSuffix("csv"); selectFile("export.csv");
        setLabelText(QFileDialog::FileType,QObject::tr("Format:"));
        connect(this,&QFileDialog::filterSelected,this,[this]{
            setDefaultSuffix(format());
            const auto path=selectedFiles().value(0);
            if(!path.isEmpty() && !QFileInfo(path).isDir()) normalizeFile(path);
        });
    }
    QString format() const {
        return QStringList{"csv","xlsx","ods"}.value(nameFilters().indexOf(selectedNameFilter()),"csv");
    }
    void accept() override {
        const auto path=selectedFiles().value(0);
        if(path.isEmpty()) return;
        // Normalize before QFileDialog's overwrite confirmation, so it covers
        // the exact destination passed to the exporter.
        if(!QFileInfo(path).isDir()) normalizeFile(path);
        QFileDialog::accept();
    }
private:
    void normalizeFile(const QString &path) {
        const auto normalized=withFormat(path);
        if(normalized==path) return;
        // QFileDialog preserves a focused filename editor on selectFile().
        auto *editor=focusWidget();
        if(editor) editor->clearFocus();
        selectFile(normalized);
        if(editor) editor->setFocus(Qt::OtherFocusReason);
    }
    QString withFormat(const QString &path) const {
        const auto suffix=QFileInfo(path).suffix();
        if(suffix.compare(format(),Qt::CaseInsensitive)==0) return path;
        if(QStringList{"csv","xlsx","ods"}.contains(suffix.toLower())) return path.left(path.size()-suffix.size())+format();
        return path+"."+format();
    }
};

class CellDelegate : public QStyledItemDelegate {
public:
    explicit CellDelegate(QObject *parent) : QStyledItemDelegate(parent) {
        auto clear=[this](QWidget *editor) {
            if (editor==m_editor) { m_editor.clear(); m_index=QPersistentModelIndex{}; }
        };
        connect(this,&QAbstractItemDelegate::commitData,this,clear);
        connect(this,&QAbstractItemDelegate::closeEditor,this,[clear](QWidget *editor,QAbstractItemDelegate::EndEditHint){clear(editor);});
    }
    std::function<bool()> canEdit;
    std::function<void(const QModelIndex &,int)> enterPressed;
    void paint(QPainter *painter,const QStyleOptionViewItem &option,const QModelIndex &index) const override {
        QStyledItemDelegate::paint(painter,option,index);
        const auto item=index.data(SheetModel::CellRole).toJsonObject();
        if(item["style"].toObject()["border"].toString()=="box") {
            painter->save(); painter->setClipRect(option.rect);
            painter->setPen(QPen(option.palette.color(QPalette::Text),2)); painter->setBrush(Qt::NoBrush);
            painter->drawRect(option.rect.adjusted(1,1,-2,-2)); painter->restore();
        }
    }
    QWidget *createEditor(QWidget *parent, const QStyleOptionViewItem &option, const QModelIndex &index) const override {
        if (canEdit && !canEdit()) return nullptr;
        auto *editor = QStyledItemDelegate::createEditor(parent, option, index);
        if (auto *line = qobject_cast<QLineEdit *>(editor)) {
            line->setMaxLength(65536); m_editor=line; m_index=index;
            m_original=index.data(Qt::EditRole).toString();
            const auto item=index.data(SheetModel::CellRole).toJsonObject();
            m_inputKind=item["input_kind"].toString("auto");
            line->setAccessibleName(tr("Cell %1%2 input").arg(SheetModel::columnName(index.column()+1))
                .arg(item["row"].toInt(index.row()+1)));
        }
        return editor;
    }
    QJsonObject takeDraft() {
        if (!m_editor || !m_index.isValid()) return {};
        QJsonObject draft;
        if (m_editor->text()!=m_original)
            draft={{"view_row",m_index.row()},{"col",m_index.column()+1},{"source",m_editor->text()},{"input_kind",m_inputKind}};
        auto *editor=m_editor.data(); m_editor.clear(); m_index=QPersistentModelIndex{};
        emit closeEditor(editor,QAbstractItemDelegate::RevertModelCache);
        return draft;
    }
protected:
    void initStyleOption(QStyleOptionViewItem *option,const QModelIndex &index) const override {
        QStyledItemDelegate::initStyleOption(option,index);
        option->features.setFlag(QStyleOptionViewItem::WrapText,index.data(SheetModel::CellRole).toJsonObject()["style"].toObject()["wrap"].toBool());
    }
    bool eventFilter(QObject *object, QEvent *event) override {
        if (object==m_editor && event->type()==QEvent::KeyPress) {
            const auto *key=static_cast<QKeyEvent *>(event);
            if (key->key()==Qt::Key_Return || key->key()==Qt::Key_Enter) {
                if (enterPressed) enterPressed(m_index,key->modifiers().testFlag(Qt::ShiftModifier)?-1:1);
                QPointer<QLineEdit> editor=m_editor;
                emit commitData(editor);
                if (editor) emit closeEditor(editor,QAbstractItemDelegate::NoHint);
                return true;
            }
        }
        return QStyledItemDelegate::eventFilter(object,event);
    }
private:
    mutable QPointer<QLineEdit> m_editor;
    mutable QPersistentModelIndex m_index;
    mutable QString m_original, m_inputKind;
};
}

MainWindow::MainWindow(const StartupOptions &options, QWidget *parent)
    : QMainWindow(parent), m_options(options), m_client(new RestClient(this)), m_model(new SheetModel(m_client, this)) {
    setObjectName("mainWindow");
    setWindowTitle("DataHorizon — Development preview");
    resize(1160, 760);
    m_service.setParent(this);
    m_service.setObjectName("ownedService");
    buildUi();
    connect(QApplication::clipboard(), &QClipboard::dataChanged, this, [this] { ++m_clipboardGeneration; });
    connect(m_model, &SheetModel::editRequested, this, [this](int row, int col, const QString &source, const QString &kind) {
        const auto destination=m_enterRequest; m_enterRequest={};
        if (m_busy || m_unknown) return;
        const QJsonObject target{{"workbook",m_info["id"]},{"sheet",m_model->sheet()},{"state",m_info["state"]},
                                 {"row",row},{"col",col},{"input_kind",kind},{"source",source},{"needs_review",false}};
        if (!resolveDraft()) return;
        QJsonObject cell{{"row", row}, {"col", col}};
        const auto index=m_model->index(m_model->viewRow(row-1),col-1);
        const auto original=m_model->cell(index);
        if (!m_model->sourceReady(index) || source!=original["source"].toString() || kind!=original["input_kind"].toString("auto")) {
            cell["source"]=source; cell["kind"]=kind;
        }
        submit({{"action", "edit"}, {"label", "Edit cell"}, {"cells", QJsonArray{cell}},
                {"workbook",target["workbook"]},{"sheet",target["sheet"]},{"expected_state",target["state"]}});
        if (!m_pendingId.isEmpty()) {
            m_recoveredDraft=target; m_draftSubmissionId=m_pendingId;
            if (!destination.isEmpty()) { m_enterNavigation=destination; m_enterNavigation["operation_id"]=m_pendingId; }
        }
    });
    connect(m_model, &SheetModel::issue, this, &MainWindow::issue);
    connect(m_model, &SheetModel::externalChange, this, [this] {
        if (!m_busy && !m_refreshing) { issue("Independent changes detected. Inputs refreshed; prior Undo history is unavailable."); refresh(); }
    });
    connect(m_model, &SheetModel::tileLoaded, this, &MainWindow::syncSource);
    if (!m_options.initialWorkbook.isEmpty()) {
        m_options.automaticWorkbook=false;
        connect(this,&MainWindow::serviceReady,this,[this]{openBook(m_options.initialWorkbook);},Qt::SingleShotConnection);
    }
    QTimer::singleShot(0, this, &MainWindow::startService);
}

MainWindow::~MainWindow() {
    saveViewState();
    m_shuttingDown = true;
    if (m_service.state() != QProcess::NotRunning) {
        m_service.terminate();
        if (!m_service.waitForFinished(1500)) { m_service.kill(); m_service.waitForFinished(1000); }
    }
}

QString MainWindow::base() const { return "/v1/workbooks/" + m_info.value("id").toString(); }

void MainWindow::buildUi() {
    auto *file = menuBar()->addMenu(tr("&File"));
    auto *edit = menuBar()->addMenu(tr("&Edit"));
    auto *formatMenu = menuBar()->addMenu(tr("F&ormat"));
    auto *sheetMenu = menuBar()->addMenu(tr("&Sheet"));
    auto *dataMenu = menuBar()->addMenu(tr("&Data"));
    auto *view = menuBar()->addMenu(tr("&View"));
    auto *help = menuBar()->addMenu(tr("&Help"));
    m_toolbar = addToolBar(tr("Workbook"));
    m_toolbar->setObjectName("workbookToolbar");
    auto add = [this](QMenu *menu, const QString &label, const QKeySequence &key, auto callback, bool toolbar = false) {
        auto *action = menu->addAction(label);
        if (!key.isEmpty()) action->setShortcut(key);
        connect(action, &QAction::triggered, this, callback);
        if (toolbar) m_toolbar->addAction(action);
        return action;
    };
    add(file, tr("&New workbook"), QKeySequence::New, [this] { if (!m_busy) newBook(); }, true);
    add(file, tr("&Open…"), QKeySequence::Open, [this] {
        if (m_busy) return;
        auto path = QFileDialog::getOpenFileName(this, tr("Open native workbook"), {}, tr("DataHorizon (*.dhwb);;All files (*)"));
        if (!path.isEmpty()) openBook(path);
    }, true);
    add(file, tr("Open read-only…"), {}, [this] {
        if (m_busy) return;
        auto path = QFileDialog::getOpenFileName(this, tr("Open read-only workbook"), {}, tr("DataHorizon (*.dhwb)"));
        if (!path.isEmpty()) openBook(path, false, true);
    });
    add(file, tr("&Save"), QKeySequence::Save, [this] { save(false); }, true)->setObjectName("saveAction");
    add(file, tr("Save a &copy…"), QKeySequence::SaveAs, [this] { save(true); });
    file->addSeparator();
    add(file, tr("Import…"), {}, [this] { importFile(); })->setObjectName("importAction");
    add(file, tr("Export…"), {}, [this] { exportFile(); })->setObjectName("exportAction");
    add(file, tr("Recovery workbooks…"), {}, [this] {
        if (m_busy) return;
        auto path = QFileDialog::getOpenFileName(this, tr("Open recovery workbook"), QStandardPaths::writableLocation(QStandardPaths::AppDataLocation)+"/recovery", tr("DataHorizon (*.dhwb)"));
        if (!path.isEmpty()) openBook(path);
    });
    add(file,tr("Inspect native/recovery file…"),{},[this]{
        if(m_busy) return;
        const auto path=QFileDialog::getOpenFileName(this,tr("Inspect native file without modifying it"),recoveryDirectory(),tr("DataHorizon (*.dhwb)"));
        if(!path.isEmpty()) inspectNativeFile(path);
    });
    file->addSeparator();
    add(file, tr("&Close"), QKeySequence::Close, [this] { close(); });
    add(file, tr("Restart local service"), {}, [this] { restartService(); })->setObjectName("restartServiceAction");
    m_undo = add(edit, tr("&Undo"), QKeySequence::Undo, [this] { submit({{"action", "undo"}}); }, true);
    m_redo = add(edit, tr("&Redo"), QKeySequence::Redo, [this] { submit({{"action", "redo"}}); });
    edit->addSeparator();
    add(edit, tr("Cu&t"), QKeySequence::Cut, [this] { copy(true); });
    add(edit, tr("&Copy"), QKeySequence::Copy, [this] { copy(); });
    add(edit, tr("&Paste"), QKeySequence::Paste, [this] { paste(); }, true)->setObjectName("pasteAction");
    add(edit, tr("Paste values only"), QKeySequence(tr("Ctrl+Shift+V")), [this] { paste(true); });
    add(edit, tr("Paste formats only"), {}, [this] { paste(false, true); });
    add(edit,tr("Clear contents"),QKeySequence(Qt::Key_Delete),[this]{clearCells("contents");})->setObjectName("clearContentsAction");
    add(edit,tr("Clear all (contents and formats)"),{},[this]{clearCells("all");})->setObjectName("clearAllAction");
    add(edit, tr("Fill from top-left"), {}, [this] {
        auto command = selectedRange(); if (command.isEmpty()) return;
        command["action"] = "fill"; command["label"] = "Fill range"; submit(command);
    });
    for(const auto &mode:QStringList{"down","right"}) {
        auto *action=add(edit,mode=="down"?tr("Fill down"):tr("Fill right"),
            QKeySequence(mode=="down"?tr("Ctrl+D"):tr("Ctrl+R")),[this,mode]{
                auto command=selectedRange();if(command.isEmpty())return;
                command=targetContext(command);command["action"]="fill";command["mode"]=mode;
                command["label"]=mode=="down"?tr("Fill down"):tr("Fill right");submit(command);
            });
        action->setObjectName(mode=="down"?"fillDownAction":"fillRightAction");
    }
    add(edit, tr("Fill numeric series…"), {}, [this] {
        if (m_busy) return;
        auto command = selectedRange(); if (command.isEmpty()) return;
        command=targetContext(command);
        bool ok; auto step = QInputDialog::getDouble(this,tr("Numeric series"),tr("Step, in row-major order"),1,-1e12,1e12,4,&ok);
        if (ok) { command["action"]="fill"; command["mode"]="series"; command["step"]=step; command["label"]="Fill series"; submit(command); }
    });
    add(edit, tr("Refresh stored data"), QKeySequence::Refresh, [this] { if (!m_busy) refresh(); });
    add(edit, tr("Find in selection…"), QKeySequence::Find, [this] { findDialog(false); })->setObjectName("findAction");
    m_findNext=add(edit,tr("Find next in results"),QKeySequence::FindNext,[this]{navigateFind(1);});
    m_findPrevious=add(edit,tr("Find previous in results"),QKeySequence::FindPrevious,[this]{navigateFind(-1);});
    m_findNext->setObjectName("findNextAction");m_findPrevious->setObjectName("findPreviousAction");
    clearFindResults();
    connect(m_model,&QAbstractItemModel::modelReset,this,&MainWindow::clearFindResults);
    add(edit, tr("Replace all in selection…"), QKeySequence::Replace, [this] { findDialog(true); });
    add(edit, tr("Reconcile unknown operation"), {}, [this] { reconcile(); });
    add(formatMenu, tr("Bold"), QKeySequence::Bold, [this] { format({{"bold", !m_model->cell(m_grid->currentIndex())["style"].toObject()["bold"].toBool()}}); }, true);
    add(formatMenu, tr("Italic"), QKeySequence::Italic, [this] { format({{"italic", !m_model->cell(m_grid->currentIndex())["style"].toObject()["italic"].toBool()}}); });
    add(formatMenu, tr("Wrap text"), {}, [this] { format({{"wrap", !m_model->cell(m_grid->currentIndex())["style"].toObject()["wrap"].toBool()}}); })->setObjectName("wrapTextAction");
    add(formatMenu, tr("All cell borders"), {}, [this] { format({{"border","box"}}); })->setObjectName("allBordersAction");
    add(formatMenu, tr("Clear cell borders"), {}, [this] { format({{"border","none"}}); })->setObjectName("clearBordersAction");
    add(formatMenu,tr("Clear formatting"),{},[this]{clearCells("formats");})->setObjectName("clearFormatsAction");
    add(formatMenu, tr("General"), {}, [this] { format({{"format", "general"}}); });
    add(formatMenu, tr("Text"), {}, [this] { format({{"format", "text"}}); });
    add(formatMenu, tr("Two decimal places"), {}, [this] { format({{"format", "fixed2"}}); }, true);
    auto *percentAction=add(formatMenu,tr("Percentage (two decimal places)"),{},[this]{format({{"format","percent2"}});});
    percentAction->setObjectName("percentFormatAction"); percentAction->setToolTip(tr("Display numeric fractions: 0.125 becomes 12.50%. Stored values stay unchanged."));
    for (const auto &align : {QString("left"), QString("center"), QString("right")})
        add(formatMenu, tr("Align %1").arg(align), {}, [this, align] { format({{"align", align}}); });
    add(formatMenu, tr("Cell background…"), {}, [this] {
        if (m_busy) return;
        auto target=selectedRange(); if (target.isEmpty()) return;
        target=targetContext(target);
        auto color = QColorDialog::getColor(Qt::white, this);
        if (color.isValid()) format({{"background", color.name()}},target);
    });
    add(sheetMenu, tr("Add sheet…"), {}, [this] {
        if (m_busy) return;
        auto command=targetContext({{"action","sheet_add"}});
        bool ok; auto name = QInputDialog::getText(this, tr("Add sheet"), tr("Name"), QLineEdit::Normal, "Sheet2", &ok);
        if (ok) { command["name"]=name; submit(command); }
    })->setObjectName("addSheetAction");
    add(sheetMenu, tr("Rename sheet…"), {}, [this] {
        if (m_busy) return;
        auto command=targetContext({{"action","sheet_rename"}});
        bool ok; auto name = QInputDialog::getText(this, tr("Rename sheet"), tr("New name (updates formula references and clears Undo history)"), QLineEdit::Normal, m_tabs->tabText(m_tabs->currentIndex()), &ok);
        if (ok) { command["name"]=name; submit(command); }
    })->setObjectName("renameSheetAction");
    add(sheetMenu, tr("Duplicate sheet…"), {}, [this] {
        if (m_busy) return;
        auto command=targetContext({{"action","sheet_duplicate"}});
        bool ok; auto name = QInputDialog::getText(this,tr("Duplicate sheet"),tr("New name (copies self-references and clears Undo history)"),QLineEdit::Normal,m_tabs->tabText(m_tabs->currentIndex())+" copy",&ok);
        if (ok) { command["name"]=name; submit(command); }
    })->setObjectName("duplicateSheetAction");
    add(sheetMenu, tr("Move sheet left"), {}, [this] {
        int index=m_tabs->currentIndex(); if(m_busy || index<=0) return;
        QStringList ids; for(int i=0;i<m_tabs->count();++i) ids.append(m_tabs->tabData(i).toString()); ids.swapItemsAt(index,index-1);
        submit({{"action","sheet_reorder"},{"order",QJsonArray::fromStringList(ids)}});
    });
    add(sheetMenu, tr("Delete sheet…"), {}, [this] {
        if (m_busy || m_info.isEmpty()) return;
        const auto command=targetContext({{"action","sheet_delete"}});
        if (QMessageBox::question(this, tr("Delete sheet"), tr("Delete this sheet and its cells? References to this sheet become #REF! and affected formulas are reported in Issues. This action clears Undo history."),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel) == QMessageBox::Yes)
            submit(command);
    })->setObjectName("deleteSheetAction");
    sheetMenu->addSeparator();
    add(sheetMenu,tr("Row height…"),{},[this]{resizeSections(false);})->setObjectName("rowHeightAction");
    add(sheetMenu,tr("Column width…"),{},[this]{resizeSections(true);})->setObjectName("columnWidthAction");
    add(sheetMenu,tr("Fit columns to visible text"),{},[this]{fitColumns();})->setObjectName("fitColumnsAction");
    add(sheetMenu,tr("Reset row and column sizes"),{},[this]{resetDimensions();})->setObjectName("resetDimensionsAction");
    add(sheetMenu,tr("Hide selected rows"),{},[this]{hideSections(false);})->setObjectName("hideRowsAction");
    add(sheetMenu,tr("Hide selected columns"),{},[this]{hideSections(true);})->setObjectName("hideColumnsAction");
    add(sheetMenu,tr("Show all hidden rows"),{},[this]{showHidden(true,false);})->setObjectName("showRowsAction");
    add(sheetMenu,tr("Show all hidden columns"),{},[this]{showHidden(false,true);})->setObjectName("showColumnsAction");
    sheetMenu->addSeparator();
    add(sheetMenu, tr("Insert selected rows…"), {}, [this] { changeStructure(false,false); })->setObjectName("insertRowsAction");
    add(sheetMenu, tr("Delete selected rows…"), {}, [this] { changeStructure(false,true); })->setObjectName("deleteRowsAction");
    add(sheetMenu, tr("Insert selected columns…"), {}, [this] { changeStructure(true,false); })->setObjectName("insertColumnsAction");
    add(sheetMenu, tr("Delete selected columns…"), {}, [this] { changeStructure(true,true); })->setObjectName("deleteColumnsAction");
    add(dataMenu, tr("Sort / filter selected list…"), {}, [this] { listDialog(); }, true);
    add(dataMenu, tr("Clear list view"), {}, [this] { if (!m_busy) { m_listSpec = {}; refresh(); } }, true)->setObjectName("clearListAction");

    auto *central = new QWidget(this); auto *layout = new QVBoxLayout(central);
    layout->setContentsMargins(4, 4, 4, 4); layout->setSpacing(4);
    auto *formula = new QHBoxLayout;
    m_address = new QLineEdit("A1"); m_address->setObjectName("address"); m_address->setAccessibleName(tr("Cell address")); m_address->setMaximumWidth(180); m_address->setToolTip(tr("Cell or range, for example B2:D20 (up to 10,000 cells)"));
    m_source = new QLineEdit; m_source->setObjectName("source"); m_source->setAccessibleName(tr("Cell input or formula")); m_source->setPlaceholderText(tr("Value or formula"));
    m_source->setMaxLength(65536);
    m_source->installEventFilter(this);
    formula->addWidget(m_address); formula->addWidget(new QLabel("fx")); formula->addWidget(m_source, 1);
    layout->addLayout(formula);
    m_grid = new QTableView; m_grid->setObjectName("grid"); m_grid->setAccessibleName(tr("Spreadsheet")); m_grid->setModel(m_model);
    auto *delegate=new CellDelegate(m_grid); delegate->canEdit=[this]{return !m_source->isModified();};
    delegate->enterPressed=[this](const QModelIndex &index,int direction){m_enterRequest=enterDestination(index,direction);};
    connect(delegate,&QAbstractItemDelegate::closeEditor,this,[this]{m_enterRequest={};});
    m_grid->setItemDelegate(delegate);
    m_grid->setSelectionMode(QAbstractItemView::ContiguousSelection);
    m_grid->setSelectionBehavior(QAbstractItemView::SelectItems);
    m_grid->horizontalHeader()->setDefaultSectionSize(110);
    m_grid->verticalHeader()->setDefaultSectionSize(26);
    m_grid->horizontalHeader()->setMinimumSectionSize(30);
    m_grid->setAlternatingRowColors(false);
    connect(m_model,&QAbstractItemModel::modelAboutToBeReset,this,[this]{
        QScopedValueRollback<bool> applying(m_applyingView,true);
        m_frozenTop->setFrozen(false);m_frozenTop->setFirstColumnFrozen(false);
        for(int row:m_hiddenViewRows) m_grid->setRowHidden(row,false);
        for(int col:m_hiddenViewColumns) m_grid->setColumnHidden(col,false);
        m_hiddenViewRows.clear(); m_hiddenViewColumns.clear();
        for(int row:m_wrappedRows) m_grid->setRowHeight(row,26);
        m_wrappedRows.clear();
        for(int row:m_sizedRows) m_grid->setRowHeight(row,26);
        for(int col:m_sizedColumns) m_grid->setColumnWidth(col,110);
        m_sizedRows.clear(); m_sizedColumns.clear();
    });
    auto *viewSaveTimer=new QTimer(this); viewSaveTimer->setSingleShot(true); viewSaveTimer->setInterval(250);
    connect(viewSaveTimer,&QTimer::timeout,this,&MainWindow::saveViewState);
    auto recordSize=[this,viewSaveTimer](bool columns,int section,int size){
        if(m_applyingView || m_viewKey.isEmpty() || size<=0) return;
        auto &sizes=columns?m_columnSizes:m_rowSizes;
        const int source=columns?section:m_model->sourceRow(section);
        if(source<0) return;
        if(!sizes.contains(source) && sizes.size()>=1024) {
            QScopedValueRollback<bool> applying(m_applyingView,true);
            if(m_frozenTop)m_frozenTop->resizeSection(columns,section,columns?110:26);
            else if(columns)m_grid->setColumnWidth(section,110);else m_grid->setRowHeight(section,26);
            issue("Only 1,024 custom sizes per axis are supported. Reset sizes before adding more."); return;
        }
        size=qBound(columns?30:18,size,columns?2000:1000);
        sizes[source]=size;
        if(columns) m_sizedColumns.insert(section); else { m_sizedRows.insert(section); m_wrappedRows.remove(section); }
        QScopedValueRollback<bool> applying(m_applyingView,true);
        if(m_frozenTop)m_frozenTop->resizeSection(columns,section,size);
        else if(columns)m_grid->setColumnWidth(section,size);else m_grid->setRowHeight(section,size);
        viewSaveTimer->start();
    };
    connect(m_grid->horizontalHeader(),&QHeaderView::sectionResized,this,[recordSize](int section,int,int size){recordSize(true,section,size);});
    connect(m_grid->verticalHeader(),&QHeaderView::sectionResized,this,[recordSize](int section,int,int size){recordSize(false,section,size);});
    auto *wrapTimer=new QTimer(this); wrapTimer->setSingleShot(true); wrapTimer->setInterval(0);
    connect(wrapTimer,&QTimer::timeout,this,&MainWindow::fitVisibleWrappedRows);
    connect(m_model,&SheetModel::tileLoaded,wrapTimer,qOverload<>(&QTimer::start));
    connect(m_grid->verticalScrollBar(),&QScrollBar::valueChanged,wrapTimer,qOverload<>(&QTimer::start));
    connect(m_grid->horizontalScrollBar(),&QScrollBar::valueChanged,wrapTimer,qOverload<>(&QTimer::start));
    connect(m_grid->horizontalHeader(),&QHeaderView::sectionResized,wrapTimer,qOverload<>(&QTimer::start));
    auto *gridArea=new QWidget(central); auto *gridLayout=new QGridLayout(gridArea);
    gridLayout->setContentsMargins(0,0,0,0); gridLayout->setSpacing(0);
    m_frozenTop=new FrozenPanes(m_grid,gridArea);
    for(auto *pane:m_frozenTop->views()) {
        connect(pane->horizontalHeader(),&QHeaderView::sectionResized,wrapTimer,qOverload<>(&QTimer::start));
        connect(pane->horizontalHeader(),&QHeaderView::sectionResized,this,[recordSize](int section,int,int size){recordSize(true,section,size);});
        connect(pane->verticalHeader(),&QHeaderView::sectionResized,this,[recordSize](int section,int,int size){recordSize(false,section,size);});
        auto *delegate=new CellDelegate(pane);delegate->canEdit=[this]{return !m_source->isModified();};
        delegate->enterPressed=[this](const QModelIndex &index,int direction){m_enterRequest=enterDestination(index,direction);};
        connect(delegate,&QAbstractItemDelegate::closeEditor,this,[this]{m_enterRequest={};});pane->setItemDelegate(delegate);
    }
    gridLayout->addWidget(m_frozenTop,0,1);gridLayout->addWidget(m_frozenTop->cornerView(),0,0);
    gridLayout->addWidget(m_frozenTop->leftWidget(),1,0);gridLayout->addWidget(m_grid,1,1);
    gridLayout->setRowStretch(1,1);gridLayout->setColumnStretch(1,1);layout->addWidget(gridArea,1);
    m_freezeAction=add(view,tr("Freeze top row"),{},[this]{
        if(m_busy || m_unknown || m_info.isEmpty() || m_model->projected() || m_hiddenRows.contains(0)) {
            QSignalBlocker blocker(m_freezeAction); m_freezeAction->setChecked(m_frozenTop->active());
            issue("Clear the list view and show row 1 before changing Freeze top row."); return;
        }
        const bool requested=m_freezeAction->isChecked(); const auto key=m_viewKey, state=m_info["state"].toString();
        captureGridDraft(false); if(!resolveDraft()) {updateFrozenRow(); return;}
        if(m_busy || m_unknown || key!=m_viewKey || state!=m_info["state"].toString() || m_model->projected() || m_hiddenRows.contains(0)) {
            updateFrozenRow(); issue("View changed. Review Freeze top row again."); return;
        }
        m_frozenRows=requested?1:0; updateFrozenRow(); saveViewState();
    });
    m_freezeAction->setCheckable(true); m_freezeAction->setObjectName("freezeTopRowAction");
    m_freezeColumnAction=add(view,tr("Freeze first column"),{},[this]{
        const bool requested=m_freezeColumnAction->isChecked();const auto target=targetContext();const auto key=m_viewKey;
        if(m_busy || m_unknown || m_info.isEmpty() || m_model->projected() || m_hiddenColumns.contains(0)) {updateFrozenRow();issue("Clear the list view and show column A before changing Freeze first column.");return;}
        captureGridDraft(false);if(!resolveDraft()){updateFrozenRow();return;}
        if(m_busy || m_unknown || !matchesTarget(target) || key!=m_viewKey || m_model->projected() || m_hiddenColumns.contains(0)) {updateFrozenRow();issue("View changed. Review Freeze first column again.");return;}
        m_frozenColumns=requested?1:0;updateFrozenRow();saveViewState();
    });
    m_freezeColumnAction->setCheckable(true);m_freezeColumnAction->setObjectName("freezeFirstColumnAction");
    m_freezePanesAction=add(view,tr("Freeze panes above and left of active cell"),{},[this]{freezeAtCurrent();});
    m_freezePanesAction->setObjectName("freezePanesAction");m_freezePanesAction->setToolTip(tr("Freeze up to 16 rows above and 8 columns left of the active cell."));
    m_unfreezePanesAction=add(view,tr("Unfreeze panes"),{},[this]{
        if(m_busy || m_unknown || m_info.isEmpty())return;
        const auto target=targetContext();captureGridDraft(false);if(!resolveDraft())return;
        if(m_busy || m_unknown || !matchesTarget(target))return;
        m_frozenRows=0;m_frozenColumns=0;updateFrozenRow();saveViewState();m_frozenTop->focusCurrent();
    });m_unfreezePanesAction->setObjectName("unfreezePanesAction");
    m_tabs = new QTabBar; m_tabs->setObjectName("sheets"); m_tabs->setAccessibleName(tr("Worksheets")); m_tabs->setExpanding(false);
    layout->addWidget(m_tabs);
    setCentralWidget(central);
    m_status = new QLabel(tr("Starting local service…")); m_status->setAccessibleName(tr("Storage and calculation status"));
    m_status->setObjectName("storageStatus");
    statusBar()->addWidget(m_status, 1);
    m_summary=new QLabel(this);m_summary->setObjectName("selectionSummary");m_summary->setAccessibleName(tr("Selection summary"));
    m_summary->setMinimumWidth(0);m_summary->setSizePolicy(QSizePolicy::Ignored,QSizePolicy::Preferred);
    statusBar()->addPermanentWidget(m_summary,1);
    m_summaryTimer=new QTimer(this);m_summaryTimer->setSingleShot(true);m_summaryTimer->setInterval(250);
    connect(m_summaryTimer,&QTimer::timeout,this,&MainWindow::requestSummary);
    connect(m_grid->selectionModel(),&QItemSelectionModel::selectionChanged,this,&MainWindow::queueSummary);
    connect(m_model,&QAbstractItemModel::modelReset,this,&MainWindow::queueSummary);
    connect(m_client,&RestClient::configured,this,[this]{
        ++m_summaryFlight;m_summaryInFlight=false;m_summaryKey={};queueSummary();
    });
    m_hiddenButton=new QPushButton(this); m_hiddenButton->setObjectName("showHiddenButton");
    m_hiddenButton->hide(); statusBar()->addPermanentWidget(m_hiddenButton);
    connect(m_hiddenButton,&QPushButton::clicked,this,[this]{showHidden(true,true);});
    auto *issuesDock = new QDockWidget(tr("Issues"), this); issuesDock->setObjectName("issuesDock");
    m_issues = new QPlainTextEdit; m_issues->setReadOnly(true); m_issues->setAccessibleName(tr("Operation and calculation issues")); issuesDock->setWidget(m_issues);
    addDockWidget(Qt::BottomDockWidgetArea, issuesDock); issuesDock->hide(); view->addAction(issuesDock->toggleViewAction());
    view->addAction(m_toolbar->toggleViewAction());
    add(help, tr("Preview capabilities"), {}, [this] {
        m_client->request("/v1/capabilities", {}, [this](QJsonObject, QString error) {
            if (!error.isEmpty()) issue(error);
            else QMessageBox::information(this, tr("Development preview"), tr("Qt Widgets/C++ desktop • SQLite • Python REST service\n\nAvailable: cell entry, bounded viewports, the published formula tier, copy/paste, fill, wrap/borders, cell Undo/Redo, bounded sheet and row/column edits, list views, scoped search, CSV and bounded XLSX/ODS selection/workbook interchange. Sheet sizes, hiding and frozen panes use local desktop preferences.\n\nUnavailable: unrestricted reference transforms, managed SQL, workbook code authoring and remote binding.\n\nThese limits are recorded in docs/design/implementation-status.md."));
        }, false);
    });
    add(help, tr("About DataHorizon"), {}, [this] { QMessageBox::about(this, tr("DataHorizon"), tr("DataHorizon 0.1 development preview\nGUI: GNU GPL version 3.\nOther component licenses remain undecided.\nSee repository licensing materials.")); });
    connect(m_grid->selectionModel(), &QItemSelectionModel::currentChanged, this, [this](const QModelIndex &, const QModelIndex &previous) {
        if (m_source->isModified() && !resolveDraft()) {
            if (previous.isValid()) { QSignalBlocker blocker(m_grid->selectionModel()); m_grid->setCurrentIndex(previous); }
            syncSource();
            return;
        }
        syncSource();
    });
    connect(m_tabs, &QTabBar::currentChanged, this, &MainWindow::changeSheet);
    connect(m_source, &QLineEdit::returnPressed, this, [this] {
        submitDraft();
    });
    connect(m_address,&QLineEdit::returnPressed,this,[this] {
        if(m_busy || m_unknown || m_info.isEmpty()) return;
        if(m_model->projected()) {issue("Clear the list view before navigating by source address.");return;}
        const auto match=QRegularExpression("^\\$?([A-Za-z]{1,3})\\$?([1-9][0-9]{0,6})(?::\\$?([A-Za-z]{1,3})\\$?([1-9][0-9]{0,6}))?$").match(m_address->text().trimmed());
        auto column=[](const QString &name){int value=0;for(auto ch:name.toUpper())value=value*26+ch.unicode()-'A'+1;return value;};
        const int row=match.captured(2).toInt(),col=column(match.captured(1));
        const int endRow=match.captured(4).isEmpty()?row:match.captured(4).toInt();
        const int endCol=match.captured(3).isEmpty()?col:column(match.captured(3));
        if(!match.hasMatch() || qMax(row,endRow)>1048576 || qMax(col,endCol)>16384) {
            issue("Use a cell or rectangular range within A1:XFD1048576, such as B2:D20.");return;
        }
        const int r1=qMin(row,endRow),r2=qMax(row,endRow),c1=qMin(col,endCol),c2=qMax(col,endCol);
        if(qint64(r2-r1+1)*(c2-c1+1)>10000) {issue("Address range exceeds the 10,000-cell selection limit.");return;}
        if(intersectsHidden(r1,c1,r2,c2)) {issue("That address range includes hidden cells. Show hidden rows or columns before navigating there.");return;}
        const auto target=targetContext();captureGridDraft(false);if(!resolveDraft())return;
        if(m_busy || !matchesTarget(target)) {issue("Navigation target changed. Enter the address again.");return;}
        m_address->setModified(false);
        const auto current=m_model->index(row-1,col-1);
        m_grid->selectionModel()->setCurrentIndex(current,QItemSelectionModel::NoUpdate);
        m_grid->selectionModel()->select(QItemSelection(m_model->index(r1-1,c1-1),m_model->index(r2-1,c2-1)),QItemSelectionModel::ClearAndSelect);
        m_frozenTop->focusCurrent();
    });
    setBusy(true);
}

void MainWindow::fitVisibleWrappedRows() {
    QScopedValueRollback<bool> applying(m_applyingView,true);
    // Only inspect loaded visible cells; never resize a million-row model to
    // contents or fetch full sources to measure presentation.
    QList<int> rows,columns;
    for(int row=0;row<m_frozenTop->frozenRows();++row)rows.append(row);
    for(int col=0;col<m_frozenTop->frozenColumns();++col)columns.append(col);
    for(int row=qMax(0,m_grid->rowAt(0));row<m_model->rowCount() && rows.size()<64;++row) {
        if(m_grid->rowViewportPosition(row)>m_grid->viewport()->height())break;
        if(!m_grid->isRowHidden(row) && !rows.contains(row))rows.append(row);
    }
    for(int col=qMax(0,m_grid->columnAt(0));col<m_model->columnCount() && columns.size()<16;++col) {
        if(m_grid->columnViewportPosition(col)>m_grid->viewport()->width())break;
        if(!m_grid->isColumnHidden(col) && !columns.contains(col))columns.append(col);
    }
    const auto previous=m_wrappedRows;
    for(int row:previous)if(!rows.contains(row)){m_frozenTop->resizeSection(false,row,26);m_wrappedRows.remove(row);}
    for(int row:rows) {
        if(m_rowSizes.contains(m_model->sourceRow(row)))continue;
        int height=26;bool loaded=false;
        for(int col:columns) {
            const auto index=m_model->index(row,col);
            if(!m_model->loaded(index)) continue;
            loaded=true;
            if(!m_model->cell(index)["style"].toObject()["wrap"].toBool()) continue;
            const QFontMetrics metrics(qvariant_cast<QFont>(m_model->data(index,Qt::FontRole)));
            const auto bounds=metrics.boundingRect(QRect(0,0,qMax(1,m_frozenTop->sectionSize(true,col)-8),0),Qt::TextWordWrap,
                                                  m_model->data(index).toString());
            height=qMax(height,qMin(260,bounds.height()+8));
        }
        if(loaded && (height>26 || m_wrappedRows.contains(row))) {
            if(m_grid->rowHeight(row)!=height) m_frozenTop->resizeSection(false,row,height);
            if(height>26) m_wrappedRows.insert(row); else m_wrappedRows.remove(row);
        }
    }
}

QString MainWindow::viewSettingsPath() const {
    if(!m_options.viewStatePath.isEmpty()) return m_options.viewStatePath;
    return qEnvironmentVariable("DATAHORIZON_VIEW_STATE",QStandardPaths::writableLocation(QStandardPaths::AppConfigLocation)+"/views.ini");
}

void MainWindow::saveViewState() {
    if(m_viewKey.isEmpty()) return;
    auto encode=[](const QMap<int,int> &sizes){
        QJsonObject result; for(auto it=sizes.begin();it!=sizes.end();++it) result[QString::number(it.key())]=it.value(); return result;
    };
    QSettings settings(viewSettingsPath(),QSettings::IniFormat);
    auto hidden=[](const QSet<int> &values){QJsonArray result; QList<int> sorted=values.values(); std::sort(sorted.begin(),sorted.end()); for(int value:sorted) result.append(value); return result;};
    settings.setValue(m_viewKey,QJsonDocument(QJsonObject{{"rows",encode(m_rowSizes)},{"columns",encode(m_columnSizes)},
        {"hidden_rows",hidden(m_hiddenRows)},{"hidden_columns",hidden(m_hiddenColumns)},{"freeze_top_row",m_frozenRows>0},{"freeze_first_column",m_frozenColumns>0},{"frozen_rows",m_frozenRows},{"frozen_columns",m_frozenColumns}}).toJson(QJsonDocument::Compact));
    settings.sync();
    if(settings.status()!=QSettings::NoError && !m_shuttingDown) issue("Could not save desktop view preferences. Workbook data remains stored.");
}

void MainWindow::restoreViewState() {
    QScopedValueRollback<bool> applying(m_applyingView,true);
    const auto identity=m_info["path"].toString()+"/"+m_info["workbook_id"].toString()+"/"+m_model->sheet();
    const auto key="views/v1/"+QString::fromLatin1(QCryptographicHash::hash(identity.toUtf8(),QCryptographicHash::Sha256).toHex());
    if(key!=m_viewKey) {
        saveViewState(); m_viewKey=key; m_rowSizes.clear(); m_columnSizes.clear();
        m_hiddenRows.clear(); m_hiddenColumns.clear();
        QSettings settings(viewSettingsPath(),QSettings::IniFormat);
        const auto bytes=settings.value(key).toByteArray();
        const auto state=bytes.size()<=256*1024?QJsonDocument::fromJson(bytes).object():QJsonObject{};
        auto decode=[](const QJsonObject &values,int maximum,int lower,int upper){
            QMap<int,int> result;
            if(values.size()>1024) return result;
            for(auto it=values.begin();it!=values.end();++it) {
                bool ok=false; const int section=it.key().toInt(&ok), size=it.value().toInt(-1);
                if(ok && section>=0 && section<maximum && it.value().isDouble() && it.value().toDouble()==size && size>=lower && size<=upper)
                    result[section]=size;
            }
            return result;
        };
        m_rowSizes=decode(state["rows"].toObject(),1048576,18,1000);
        m_columnSizes=decode(state["columns"].toObject(),16384,30,2000);
        auto hidden=[](const QJsonArray &values,int maximum){
            QSet<int> result; if(values.size()>1024) return result;
            for(const auto &value:values) if(value.isDouble() && value.toDouble()==value.toInt(-1) && value.toInt()>=0 && value.toInt()<maximum) result.insert(value.toInt());
            return result;
        };
        m_hiddenRows=hidden(state["hidden_rows"].toArray(),1048576);
        m_hiddenColumns=hidden(state["hidden_columns"].toArray(),16384);
        auto count=[](QJsonValue value,int maximum,int fallback){const int n=value.toInt(-1);return value.isDouble() && value.toDouble()==n && n>=0 && n<=maximum?n:fallback;};
        m_frozenRows=count(state["frozen_rows"],16,state["freeze_top_row"].toBool()?1:0);
        m_frozenColumns=count(state["frozen_columns"],8,state["freeze_first_column"].toBool()?1:0);
    }
    for(auto it=m_rowSizes.begin();it!=m_rowSizes.end();++it) {
        const int row=m_model->viewRow(it.key());
        if(row>=0 && row<m_model->rowCount()) { m_grid->setRowHeight(row,it.value()); m_sizedRows.insert(row); }
    }
    for(auto it=m_columnSizes.begin();it!=m_columnSizes.end();++it) {
        m_grid->setColumnWidth(it.key(),it.value()); m_sizedColumns.insert(it.key());
    }
    applyVisibility();
}

QPair<int,int> MainWindow::selectedSections(bool columns) {
    if(m_busy || m_unknown || m_info.isEmpty()) return {-1,-1};
    const auto ranges=m_grid->selectionModel()->selection();
    if(ranges.isEmpty()) return {-1,-1};
    const int first=columns?ranges.first().left():ranges.first().top();
    const int last=columns?ranges.last().right():ranges.last().bottom();
    if(last-first>=1024) { issue("Select at most 1,024 rows or columns for this view operation."); return {-1,-1}; }
    return {first,last};
}

void MainWindow::resizeSections(bool columns) {
    const auto sections=selectedSections(columns); if(sections.first<0) return;
    auto sizes=columns?m_columnSizes:m_rowSizes; QList<int> sources;
    for(int section=sections.first;section<=sections.second;++section) {
        const int source=columns?section:m_model->sourceRow(section); sources.append(source); sizes[source]=1;
    }
    if(sizes.size()>1024) { issue("This selection exceeds the 1,024 custom sizes per axis. Reset sizes first."); return; }
    // Capture the source identity across the modal event loop.
    const auto key=m_viewKey; const auto state=m_info["state"];
    bool ok=false;
    const int size=QInputDialog::getInt(this,columns?tr("Column width"):tr("Row height"),
        tr("Logical pixels (saved on this computer)"),m_frozenTop->sectionSize(columns,sections.first),
        columns?30:18,columns?2000:1000,1,&ok);
    if(!ok) return;
    if(key!=m_viewKey || state!=m_info["state"] || m_busy || m_unknown) { issue("View changed while the size dialog was open. Select the rows or columns again."); return; }
    for(int section=sections.first;section<=sections.second;++section)
        if(!columns && m_model->sourceRow(section)!=sources[section-sections.first]) { issue("List order changed. Select the rows again."); return; }
    for(int section=sections.first;section<=sections.second;++section) {
        m_frozenTop->resizeSection(columns,section,size);
        const int source=sources[section-sections.first];
        if(columns){m_columnSizes[source]=size;m_sizedColumns.insert(section);}
        else {m_rowSizes[source]=size;m_sizedRows.insert(section);m_wrappedRows.remove(section);}
    }
    saveViewState();
}

void MainWindow::fitColumns() {
    const auto sections=selectedSections(true); if(sections.first<0) return;
    if(sections.second-sections.first>=16) { issue("Fit at most 16 columns using loaded visible text."); return; }
    const int first=qMax(0,m_grid->rowAt(0));
    QMap<int,int> widths;
    for(int col=sections.first;col<=sections.second;++col) {
        int width=m_grid->fontMetrics().horizontalAdvance(SheetModel::columnName(col+1))+16;
        bool loaded=false;
        for(int row=first;row<qMin(first+64,m_model->rowCount());++row) {
            if(m_grid->rowViewportPosition(row)>m_grid->viewport()->height()) break;
            const auto index=m_model->index(row,col); if(!m_model->loaded(index)) continue;
            loaded=true;
            const QFontMetrics metrics(qvariant_cast<QFont>(m_model->data(index,Qt::FontRole)));
            for(const auto &line:m_model->data(index).toString().split('\n')) width=qMax(width,metrics.horizontalAdvance(line)+16);
        }
        if(!loaded) { issue("Load the selected columns before fitting. Fit samples only visible preview text."); return; }
        widths[col]=qBound(30,width,1000);
    }
    auto sizes=m_columnSizes; for(auto it=widths.begin();it!=widths.end();++it) sizes[it.key()]=it.value();
    if(sizes.size()>1024) { issue("Fit exceeds the 1,024 custom widths. Reset sizes first."); return; }
    for(auto it=widths.begin();it!=widths.end();++it){m_frozenTop->resizeSection(true,it.key(),it.value());m_columnSizes[it.key()]=it.value();m_sizedColumns.insert(it.key());}
    saveViewState();
}

void MainWindow::resetDimensions() {
    if(m_busy || m_unknown || m_info.isEmpty()) return;
    QScopedValueRollback<bool> applying(m_applyingView,true);
    for(int row:m_sizedRows) m_frozenTop->resizeSection(false,row,26);
    for(int col:m_sizedColumns) m_frozenTop->resizeSection(true,col,110);
    m_rowSizes.clear(); m_columnSizes.clear(); m_sizedRows.clear(); m_sizedColumns.clear();
    saveViewState(); fitVisibleWrappedRows();
    updateFrozenRow();
}

bool MainWindow::intersectsHidden(int r1,int c1,int r2,int c2) const {
    for(int row:m_hiddenRows) if(row+1>=r1 && row+1<=r2) return true;
    for(int col:m_hiddenColumns) if(col+1>=c1 && col+1<=c2) return true;
    return false;
}

void MainWindow::ensureVisibleCurrent() {
    const auto current=m_grid->currentIndex(); if(!current.isValid()) return;
    int row=current.row(), col=current.column();
    auto hiddenRow=[this](int index){return m_grid->isRowHidden(index) && !(index<m_frozenTop->frozenRows());};
    auto hiddenColumn=[this](int index){return m_grid->isColumnHidden(index) && !(index<m_frozenTop->frozenColumns());};
    if(!hiddenRow(row) && !hiddenColumn(col)) return;
    for(int attempts=0;attempts<=1024 && hiddenRow(row);++attempts) row=(row+1)%qMax(1,m_model->rowCount());
    for(int attempts=0;attempts<=1024 && hiddenColumn(col);++attempts) col=(col+1)%m_model->columnCount();
    m_grid->setCurrentIndex(m_model->index(row,col));
}

void MainWindow::applyVisibility() {
    QScopedValueRollback<bool> applying(m_applyingView,true);
    for(int row:m_hiddenViewRows) m_grid->setRowHidden(row,false);
    for(int col:m_hiddenViewColumns) m_grid->setColumnHidden(col,false);
    m_hiddenViewRows.clear(); m_hiddenViewColumns.clear();
    for(int source:m_hiddenRows) {
        const int row=m_model->viewRow(source);
        if(row>=0 && row<m_model->rowCount()) {m_grid->setRowHidden(row,true); m_hiddenViewRows.insert(row);}
    }
    // A projection can exclude every visible source row. Keep one displayed row
    // reachable without changing the stored source-coordinate preferences.
    if(m_model->rowCount()>0 && m_hiddenViewRows.size()==m_model->rowCount()) {
        const int row=0; m_hiddenRows.remove(m_model->sourceRow(row));
        m_grid->setRowHidden(row,false); m_hiddenViewRows.remove(row);
        issue("This list would hide every row. Its first source row was shown.");
    }
    for(int col:m_hiddenColumns) {m_grid->setColumnHidden(col,true); m_hiddenViewColumns.insert(col);}
    m_model->setHiddenSections(m_hiddenRows,m_hiddenColumns);
    const bool any=!m_hiddenRows.isEmpty() || !m_hiddenColumns.isEmpty();
    QStringList sections;
    if(!m_hiddenRows.isEmpty()) sections.append(m_hiddenRows.size()==1?tr("1 row"):tr("%1 rows").arg(m_hiddenRows.size()));
    if(!m_hiddenColumns.isEmpty()) sections.append(m_hiddenColumns.size()==1?tr("1 column"):tr("%1 columns").arg(m_hiddenColumns.size()));
    m_hiddenButton->setText(tr("Show hidden: %1").arg(sections.join(", ")));
    m_hiddenButton->setAccessibleName(m_hiddenButton->text());
    m_hiddenButton->setToolTip(tr("Show all hidden rows and columns on this sheet.")); m_hiddenButton->setVisible(any);
    updateFrozenRow();
    ensureVisibleCurrent();queueSummary();
}

void MainWindow::freezeAtCurrent() {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    if(m_model->projected()){issue("Clear the list view before freezing panes.");return;}
    const auto cell=m_grid->currentIndex();if(!cell.isValid())return;
    const int rows=cell.row(),columns=cell.column();
    if(rows>16 || columns>8){issue("Freeze at most 16 rows and 8 columns. Select a cell nearer the top-left.");return;}
    for(int row:m_hiddenRows)if(row<rows){issue("Show hidden rows in the frozen prefix first.");return;}
    for(int col:m_hiddenColumns)if(col<columns){issue("Show hidden columns in the frozen prefix first.");return;}
    int height=0,width=0;
    for(int row=0;row<rows;++row)height+=m_frozenTop->sectionSize(false,row);
    for(int col=0;col<columns;++col)width+=m_frozenTop->sectionSize(true,col);
    if((rows && height+m_grid->horizontalHeader()->height()+54>m_grid->parentWidget()->height()) ||
       (columns && width+m_grid->verticalHeader()->width()+82>m_grid->parentWidget()->width())) {
        issue("The frozen prefix would leave too little scrolling space. Resize the window or freeze fewer rows/columns.");return;
    }
    const auto target=targetContext();const auto key=m_viewKey;
    const auto hiddenRows=m_hiddenRows,hiddenColumns=m_hiddenColumns;const auto rowSizes=m_rowSizes,columnSizes=m_columnSizes;
    const auto area=m_grid->parentWidget()->size();
    captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || m_unknown || !matchesTarget(target) || key!=m_viewKey || m_model->projected()
        || hiddenRows!=m_hiddenRows || hiddenColumns!=m_hiddenColumns || rowSizes!=m_rowSizes || columnSizes!=m_columnSizes || area!=m_grid->parentWidget()->size()) {
        issue("View changed. Select the freeze position again.");return;
    }
    m_frozenRows=rows;m_frozenColumns=columns;updateFrozenRow();saveViewState();m_frozenTop->focusCurrent();
}

void MainWindow::updateFrozenRow() {
    QScopedValueRollback<bool> applying(m_applyingView,true);
    bool rows=m_frozenRows>0 && !m_model->projected(),columns=m_frozenColumns>0 && !m_model->projected();
    for(int row:m_hiddenRows)if(row<m_frozenRows)rows=false;
    for(int col:m_hiddenColumns)if(col<m_frozenColumns)columns=false;
    int height=0,width=0;
    if(rows)for(int row=0;row<m_frozenRows;++row)height+=m_frozenTop->sectionSize(false,row);
    if(columns)for(int col=0;col<m_frozenColumns;++col)width+=m_frozenTop->sectionSize(true,col);
    const bool rowRoom=!(rows && m_frozenRows>1 && height+m_grid->horizontalHeader()->height()+54>m_grid->parentWidget()->height());
    const bool columnRoom=!(columns && m_frozenColumns>1 && width+m_grid->verticalHeader()->width()+82>m_grid->parentWidget()->width());
    if(!rowRoom || !columnRoom){
        if(!m_frozenSizeIssue)issue("Saved frozen prefixes do not fit this window. Enlarge it and reapply freezing, or choose Unfreeze panes.");
        m_frozenSizeIssue=true;
    } else m_frozenSizeIssue=false;
    rows=rows && rowRoom;columns=columns && columnRoom;
    m_frozenTop->setFrozenCounts(rows?m_frozenRows:0,columns?m_frozenColumns:0);
    m_frozenTop->setHiddenRows(m_hiddenViewRows);m_frozenTop->setHiddenColumns(m_hiddenColumns);
    if(m_freezePanesAction)m_freezePanesAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected());
    if(m_unfreezePanesAction)m_unfreezePanesAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && (m_frozenRows>0 || m_frozenColumns>0));
    if(m_freezeAction){QSignalBlocker blocker(m_freezeAction);m_freezeAction->setChecked(rows && m_frozenRows==1);
        m_freezeAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected() && !m_hiddenRows.contains(0));}
    if(m_freezeColumnAction){QSignalBlocker blocker(m_freezeColumnAction);m_freezeColumnAction->setChecked(columns && m_frozenColumns==1);
        m_freezeColumnAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected() && !m_hiddenColumns.contains(0));}
}

void MainWindow::hideSections(bool columns) {
    const auto sections=selectedSections(columns); if(sections.first<0) return;
    const auto key=m_viewKey, state=m_info["state"].toString();
    QSet<int> sources;
    for(int section=sections.first;section<=sections.second;++section) sources.insert(columns?section:m_model->sourceRow(section));
    captureGridDraft(false); if(!resolveDraft()) return;
    if(m_busy || m_unknown || key!=m_viewKey || state!=m_info["state"].toString()) {issue("View changed. Select the rows or columns again."); return;}
    auto hidden=columns?m_hiddenColumns:m_hiddenRows; hidden.unite(sources);
    if(hidden.size()>1024) {issue("At most 1,024 hidden rows or columns per axis are supported."); return;}
    int displayed=0;
    for(int source:hidden) if(columns || m_model->viewRow(source)>=0) ++displayed;
    if(displayed>=(columns?m_model->columnCount():m_model->rowCount())) {issue("Keep at least one displayed row or column visible."); return;}
    if(columns) m_hiddenColumns=hidden; else m_hiddenRows=hidden;
    applyVisibility(); saveViewState();
}

void MainWindow::showHidden(bool rows,bool columns) {
    if(m_busy || m_unknown || m_info.isEmpty()) return;
    if(rows) m_hiddenRows.clear();
    if(columns) m_hiddenColumns.clear();
    applyVisibility(); saveViewState();
}

void MainWindow::startService() {
    if (!m_options.url.isEmpty()) {
        auto url = QUrl(m_options.url);
        if (url.scheme() != "http" || (url.host() != "127.0.0.1" && url.host() != "localhost") || m_options.token.isEmpty()) {
            issue("Attach requires a localhost HTTP URL and DATAHORIZON_TOKEN. Remote connections are unavailable."); return;
        }
        m_client->configure(m_options.url, m_options.token);
        m_client->request("/v1/capabilities", {}, [this](QJsonObject info, QString error) {
            if (!error.isEmpty() || info["api_version"].toInt() != 1) { issue("Incompatible or unavailable service: " + error); return; }
            setBusy(false, "Connected to independently running service"); emit serviceReady();
            if (m_options.automaticWorkbook) newBook();
        }, false);
        return;
    }
    m_service.disconnect(this);
    m_startup.clear();
    m_client->configure({},{});
    const auto paths=resolveServicePaths(QCoreApplication::applicationDirPath(),qEnvironmentVariable("DATAHORIZON_PYTHON"));
    if(!paths.error.isEmpty()) {issue(paths.error);return;}
    m_service.setProcessEnvironment(QProcessEnvironment::systemEnvironment());
    connect(&m_service, &QProcess::readyReadStandardOutput, this, [this] {
        if(!m_client->url().isEmpty()) { m_service.readAllStandardOutput(); return; }
        m_startup += m_service.readAllStandardOutput();
        int newline = m_startup.indexOf('\n'); if (newline < 0) return;
        auto ready = QJsonDocument::fromJson(m_startup.left(newline)).object();
        m_startup.clear();
        if (ready["api_version"].toInt() != 1 || ready["token"].toString().isEmpty()) { issue("Service startup protocol is incompatible."); return; }
        m_client->configure(ready["url"].toString(), ready["token"].toString());
        setBusy(false, "Local service ready"); emit serviceReady();
        if (!m_restartPath.isEmpty()) {
            const auto path = m_restartPath; m_restartPath.clear();
            setBusy(true,"Reopening workbook after service restart…");
            m_client->request("/v1/open",{{"path",path},{"readonly",m_info["readonly"]}},[this](QJsonObject info,QString error){
                if (!error.isEmpty()) { issue(error); return; }
                if (m_pendingId.isEmpty()) acceptInfo(info);
                else { m_info=info; reconcile(); }
            });
        } else if (m_options.automaticWorkbook) newBook();
    });
    connect(&m_service, &QProcess::readyReadStandardError, this, [this] { issue(QString::fromUtf8(m_service.readAllStandardError())); });
    connect(&m_service, &QProcess::errorOccurred, this, [this] { issue("Service startup failed: " + m_service.errorString()); });
    connect(&m_service, &QProcess::finished, this, [this](int, QProcess::ExitStatus) {
        if (m_shuttingDown) return;
        m_enterRequest={}; m_enterNavigation={};
        captureGridDraft();
        m_unknown = !m_pendingId.isEmpty(); setBusy(true, "Service stopped • Restart local service or close the window");
        if (m_unknown) preserveUnknownOperation();
        issue(m_unknown ? "The local service exited. Operation outcome is unknown: "+m_pendingId
                        : "The local service exited. Restart it to reopen stored data; unsubmitted drafts are retained.");
    });
    QStringList args{"-I", "-B", "-X", "utf8", paths.launcher};
    if (!m_options.handler.isEmpty()) args << "--handler" << m_options.handler;
    m_service.start(paths.python, args);
}

void MainWindow::newBook() {
    const auto dir = recoveryDirectory();
    if (!QDir().mkpath(dir)) { issue("Cannot create recovery directory: " + dir); return; }
    openBook(dir + "/untitled-" + QUuid::createUuid().toString(QUuid::WithoutBraces) + ".dhwb", true);
}

void MainWindow::openBook(const QString &path, bool create, bool readonly) {
    if (m_busy || m_unknown) return;
    captureGridDraft(false);
    if (m_source->isModified() || !m_recoveredDraft.isEmpty()) {
        if (QMessageBox::question(this,tr("Discard editor draft?"),tr("Discard the unsubmitted formula-bar draft before opening another workbook? Stored edits remain in the current workbook."))!=QMessageBox::Yes) return;
        m_source->setModified(false); clearRecoveredDraft();
    }
    m_enterRequest={}; m_enterNavigation={};
    setBusy(true, "Opening workbook…");
    m_client->request("/v1/open", {{"path", path}, {"create", create}, {"readonly", readonly}}, [this](QJsonObject info, QString error) {
        if (!error.isEmpty()) { setBusy(false, "Open failed"); issue(error); return; }
        if (!m_info.isEmpty()) m_client->request(base()+"/close",{},[](QJsonObject,QString){});
        m_listSpec = {}; acceptInfo(info);
    });
}

void MainWindow::acceptInfo(const QJsonObject &info, const QString &selectSheet) {
    QScopedValueRollback<bool> applying(m_applyingView,true);
    const auto oldSheet = m_restoreDraft ? m_recoveredDraft["sheet"].toString() : selectSheet.isEmpty() ? m_model->sheet() : selectSheet;
    if (m_restoreDraft) m_listSpec={};
    const auto previous = m_grid->currentIndex();
    const int previousRow=previous.isValid()?m_model->sourceRow(previous.row()):0;
    const int previousColumn=previous.isValid()?previous.column():0;
    if (info["id"]!=m_info["id"]) m_released=false;
    m_info = info;
    m_tabs->blockSignals(true);
    while (m_tabs->count()) m_tabs->removeTab(0);
    int current = 0;
    for (const auto &value : info["sheets"].toArray()) {
        auto sheet = value.toObject(); int index = m_tabs->addTab(sheet["name"].toString()); m_tabs->setTabData(index, sheet["id"].toString());
        if (sheet["id"].toString() == oldSheet) current = index;
    }
    m_tabs->setCurrentIndex(current); m_tabs->blockSignals(false);
    if (!oldSheet.isEmpty() && m_tabs->tabData(current).toString()!=oldSheet) m_listSpec={};
    m_model->bind(info["id"].toString(), m_tabs->tabData(current).toString(), info["state"].toString(), info["readonly"].toBool());
    restoreViewState();
    m_grid->setCurrentIndex(m_model->index(previousRow,previousColumn));
    m_undo->setText(info["undo"].isString() ? "Undo " + info["undo"].toString() : "Undo");
    m_redo->setText(info["redo"].isString() ? "Redo " + info["redo"].toString() : "Redo");
    setBusy(false, info["readonly"].toBool() ? "Read-only • Calculations evaluated on read" : "Stored automatically • Calculations evaluated on read");
    m_undo->setEnabled(info["undo"].isString() && !info["readonly"].toBool());
    m_redo->setEnabled(info["redo"].isString() && !info["readonly"].toBool());
    setWindowTitle(QFileInfo(info["path"].toString()).fileName() + " — DataHorizon development preview");
    syncSource();
    if (m_restoreDraft) restoreRecoveredDraft();
    if (!m_listSpec.isEmpty()) applyList(); else { applyEnterNavigation(); emit workbookReady(); }
    if (m_saveMode) QTimer::singleShot(0,this,[this]{save(m_saveMode==2);});
}

void MainWindow::refresh(const QString &selectSheet) {
    if (m_info.isEmpty() || m_unknown || m_refreshing) return;
    captureGridDraft(false);
    if (!resolveDraft()) return;
    m_refreshing = true; setBusy(true, "Refreshing stored inputs…");
    const auto workbook=m_info["id"].toString();
    m_client->request(base(), {}, [this, selectSheet, workbook](QJsonObject info, QString error) {
        if (m_info["id"].toString()!=workbook) return;
        m_refreshing = false;
        if (!error.isEmpty()) { setBusy(true, "Refresh failed • Editing stopped"); issue(error); return; }
        acceptInfo(info, selectSheet);
    }, false);
}

void MainWindow::submit(QJsonObject operation) {
    if (m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool()) return;
    const auto defaults=targetContext();
    for (auto field=defaults.begin(); field!=defaults.end(); ++field)
        if (!operation.contains(field.key())) operation[field.key()]=field.value();
    captureGridDraft(false);
    if (!resolveDraft()) return;
    if (!matchesTarget(operation)) {
        issue("Reviewed inputs changed. Review the target and state again before submitting."); return;
    }
    operation.remove("workbook");
    m_pendingId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    operation["operation_id"] = m_pendingId; operation["expected_state"] = m_info["state"];
    operation["sheet"] = m_model->sheet();
    setBusy(true, "Submitting • " + m_pendingId);
    const auto operationId=m_pendingId;
    m_client->request(base()+"/operations", operation, [this,operationId](QJsonObject result, QString error) {
        if (m_pendingId!=operationId) return;
        const bool invalidAcknowledgement = error.isEmpty() && !RestClient::validReceipt(result, m_pendingId, true);
        if (invalidAcknowledgement) error = "Invalid mutation acknowledgement; reconcile its operation ID";
        if (!error.isEmpty()) {
            m_enterNavigation={};
            const auto outcome = result["outcome"].toString();
            if (!invalidAcknowledgement && (outcome == "rolled_back" || outcome == "rejected")
                && result["code"].isString() && !result["code"].toString().isEmpty() && result["message"].isString()
                && result["operation_id"].toString() == m_pendingId) {
                m_saveMode = 0;
                clearRecoveryRecord(); m_pendingId.clear(); m_unknown=false;
                const bool restoreDraft=m_draftSubmissionId==operationId && !m_recoveredDraft.isEmpty();
                if (restoreDraft) {
                    m_draftSubmissionId.clear(); m_restoreDraft=true;
                    if (result["code"]=="STALE_STATE") m_recoveredDraft["needs_review"]=true;
                }
                const bool stopped=m_options.url.isEmpty() && m_service.state()==QProcess::NotRunning;
                setBusy(stopped, stopped ? "Service stopped • Edit rejected • Restart local service" : outcome == "rolled_back" ? "Rolled back • No cells changed" : "Rejected • No cells changed"); issue(error);
                if (result["code"] == "STALE_STATE" || restoreDraft) refresh();
            } else {
                m_unknown = true; setBusy(true, "Outcome unknown • Reconcile before editing");
                preserveUnknownOperation();
                issue("Operation " + m_pendingId + ": " + error); reconcile();
            }
            emit operationFinished(false); return;
        }
        if (m_enterNavigation["operation_id"].toString()==operationId) m_enterNavigation["ready"]=true;
        if (m_draftSubmissionId==operationId) clearRecoveredDraft();
        clearRecoveryRecord(); m_pendingId.clear(); m_unknown = false; m_info["state"] = result["state"];
        reportReferenceWarnings(result);
        refresh(); emit operationFinished(true);
    });
}

void MainWindow::reconcile() {
    if (m_pendingId.isEmpty()) return;
    const auto operationId=m_pendingId;
    m_client->request(base()+"/operations/"+QString::fromLatin1(QUrl::toPercentEncoding(m_pendingId)), {}, [this,operationId](QJsonObject result, QString error) {
        if(m_pendingId!=operationId) return;
        if (!error.isEmpty()) { issue("Outcome remains unknown for " + m_pendingId + ". " + error); return; }
        if (!RestClient::validReceipt(result, m_pendingId)) { issue("Unrecognized operation receipt; editing remains stopped."); return; }
        if (m_draftSubmissionId==operationId) {
            if (result["outcome"]=="committed") clearRecoveredDraft();
            else { m_draftSubmissionId.clear(); m_restoreDraft=true; m_recoveredDraft["needs_review"]=true; }
        }
        clearRecoveryRecord(); m_pendingId.clear(); m_unknown = false;
        issue(result["outcome"]=="committed" ? "Reconciled committed operation without repeating Python handlers." : "Reconciled rollback without repeating Python handlers: "+result["message"].toString());
        reportReferenceWarnings(result);
        refresh();
    }, false);
}

QString MainWindow::recoveryDirectory() const {
    return m_options.recoveryDirectory.isEmpty() ? QStandardPaths::writableLocation(QStandardPaths::AppDataLocation)+"/recovery" : m_options.recoveryDirectory;
}

bool MainWindow::preserveUnknownOperation() {
    if (m_pendingId.isEmpty()) return false;
    const auto dir=recoveryDirectory();
    if (!QDir().mkpath(dir)) { issue("Cannot create operation recovery directory: "+dir); return false; }
    if (m_recoveryRecord.isEmpty()) m_recoveryRecord=dir+"/operation-"+QUuid::createUuid().toString(QUuid::WithoutBraces)+".json";
    QSaveFile file(m_recoveryRecord);
    const auto data=QJsonDocument(QJsonObject{{"workbook_path",m_info["path"]},{"operation_id",m_pendingId},{"outcome","unknown"},{"editor_draft",m_recoveredDraft},
        {"instructions","Reopen the workbook and look up this operation ID before retrying any mutation."}}).toJson();
    if (!file.open(QIODevice::WriteOnly)) { issue(file.errorString()); return false; }
    file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
    if (file.write(data)!=data.size() || !file.commit()) { issue(file.errorString()); return false; }
    return true;
}

void MainWindow::clearRecoveryRecord() {
    if (!m_recoveryRecord.isEmpty()) QFile::remove(m_recoveryRecord);
    m_recoveryRecord.clear();
}

void MainWindow::restartService() {
    if (!m_options.url.isEmpty()) { issue("Restart the independently running service separately; this window does not own it."); return; }
    if (m_service.state()!=QProcess::NotRunning) return;
    if (m_source->isModified()) {
        m_recoveredDraft=m_draftTarget; m_recoveredDraft["source"]=m_source->text(); m_recoveredDraft["needs_review"]=true;
        m_source->setModified(false);
    }
    if (!m_recoveredDraft.isEmpty()) { m_restoreDraft=true; m_recoveredDraft["needs_review"]=true; }
    m_restartPath=m_info["path"].toString();
    m_refreshing=false;
    setBusy(true,"Restarting local service…");
    startService();
}

void MainWindow::setBusy(bool busy, const QString &message) {
    m_busy = busy || m_releasing;
    if(m_busy) clearFindResults();
    m_model->setBusy(m_busy); m_toolbar->setEnabled(!m_busy); m_tabs->setEnabled(!m_busy);
    if(m_freezePanesAction)m_freezePanesAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected());
    if(m_unfreezePanesAction)m_unfreezePanesAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && (m_frozenRows>0 || m_frozenColumns>0));
    if(m_freezeColumnAction)m_freezeColumnAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected() && !m_hiddenColumns.contains(0));
    if(m_freezeAction) m_freezeAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected() && !m_hiddenRows.contains(0));
    syncSource();
    if (!message.isEmpty()) m_status->setText(m_releasing ? "Closing workbook…" : message);
    queueSummary();
}

void MainWindow::issue(const QString &message) {
    m_issues->appendPlainText(message);
    if (auto *dock = qobject_cast<QDockWidget *>(m_issues->parentWidget())) dock->show();
}

void MainWindow::syncSource() {
    ensureVisibleCurrent();
    const bool addressDraft=m_address->hasFocus() && m_address->isModified();
    auto index = m_grid->currentIndex();
    if (!index.isValid()) {
        m_source->clear(); m_source->setModified(false); m_source->setEnabled(false); m_draftTarget={};
        if(!addressDraft) m_address->clear();
        return;
    }
    m_model->ensureSource(index);
    const bool ready=m_model->sourceReady(index);
    m_source->setEnabled(!m_busy && !m_info["readonly"].toBool() && ready);
    if(!addressDraft) m_address->setText(SheetModel::columnName(index.column()+1) + QString::number(m_model->sourceRow(index.row())+1));
    if (!m_source->isModified()) {
        if (!ready) { m_source->clear(); m_source->setModified(false); m_draftTarget={}; m_source->setPlaceholderText("Loading cell input…"); return; }
        m_source->setPlaceholderText(tr("Value or formula"));
        m_source->setText(m_model->cell(index)["source"].toString()); m_source->setModified(false);
        m_draftTarget = {{"workbook", m_info["id"]}, {"sheet", m_model->sheet()}, {"state", m_info["state"]},
                         {"row", m_model->sourceRow(index.row())+1}, {"col", index.column()+1}, {"input_kind",m_model->cell(index)["input_kind"]}};
    }
    if (m_source->isModified() && m_draftTarget["sheet"].toString()!=m_model->sheet()) {
        if(!addressDraft) m_address->setText(SheetModel::columnName(m_draftTarget["col"].toInt())+QString::number(m_draftTarget["row"].toInt()));
        m_status->setText("Draft target unavailable • Copy the retained input or press Escape to discard it");
        return;
    }
    auto cell = m_model->cell(index);
    if (cell.contains("error")) m_status->setText(cell["display"].toString() + " • " + cell["error"].toString() + " • Input retained");
}

QJsonObject MainWindow::enterDestination(const QModelIndex &index, int direction) const {
    const auto next=m_model->index(index.row()+direction,index.column());
    if (!index.isValid() || !next.isValid()) return {};
    return {{"workbook",m_info["id"]},{"sheet",m_model->sheet()},
            {"row",m_model->sourceRow(next.row())+1},{"col",next.column()+1}};
}

void MainWindow::applyEnterNavigation() {
    if (!m_enterNavigation["ready"].toBool()) return;
    const auto target=m_enterNavigation; m_enterNavigation={};
    if (target["workbook"]!=m_info["id"] || target["sheet"].toString()!=m_model->sheet() || m_source->isModified()) return;
    const auto next=m_model->index(m_model->viewRow(target["row"].toInt()-1),target["col"].toInt()-1);
    if (next.isValid()) { m_grid->setCurrentIndex(next); m_frozenTop->focusCurrent(); syncSource(); }
}

void MainWindow::submitDraft(bool advance, int direction) {
    if (m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool()) return;
    if (!m_grid->currentIndex().isValid() || m_draftTarget["row"].toInt()<1 || m_draftTarget["col"].toInt()<1) {
        issue("Select a cell with a loaded input before editing."); return;
    }
    if (m_draftTarget["workbook"] != m_info["id"] || m_draftTarget["sheet"].toString() != m_model->sheet()
        || m_draftTarget["state"] != m_info["state"]) {
        issue("The draft target changed. Copy the draft text or press Escape, then review the current cell before submitting."); return;
    }
    const auto draft=m_draftTarget;
    const auto source=m_source->text();
    const auto destination=advance?enterDestination(m_grid->currentIndex(),direction):QJsonObject{};
    if (m_draftNeedsReview) {
        const auto target=m_model->index(m_draftTarget["row"].toInt()-1,m_draftTarget["col"].toInt()-1);
        if (!m_model->sourceReady(target)) { m_model->ensureSource(target); issue("Wait for the reopened cell input before reviewing the draft."); return; }
        const auto current=m_model->cell(target)["source"].toString();
        const auto answer=QMessageBox::question(this,tr("Review recovered draft"),
            tr("The workbook was reopened. Current input at %1%2:\n%3\n\nApply the recovered draft to this cell?\n%4")
                .arg(SheetModel::columnName(target.column()+1)).arg(target.row()+1).arg(current.left(256),m_source->text().left(256)),
            QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel);
        if (answer!=QMessageBox::Yes) return;
        if (m_draftTarget!=draft || m_source->text()!=source || draft["workbook"]!=m_info["id"]
            || draft["sheet"].toString()!=m_model->sheet() || draft["state"]!=m_info["state"]) {
            issue("Reviewed draft inputs changed. Review the current target again before applying it."); return;
        }
        m_draftNeedsReview=false;
    }
    m_recoveredDraft=m_draftTarget; m_recoveredDraft["source"]=m_source->text(); m_recoveredDraft["needs_review"]=false;
    auto cell = QJsonObject{{"row",draft["row"]},{"col",draft["col"]}};
    const auto index=m_model->index(m_model->viewRow(draft["row"].toInt()-1),draft["col"].toInt()-1);
    const auto original=m_model->cell(index);
    const auto inputKind=draft["input_kind"].toString("auto");
    const bool sameSource=m_model->sourceReady(index) && source==original["source"].toString();
    if (!sameSource || inputKind!=original["input_kind"].toString("auto")) {
        cell["source"]=source; cell["kind"]=sameSource?inputKind:inputKind=="text"?QString("text"):QString("auto");
    }
    m_source->setModified(false);
    submit({{"action","edit"},{"label","Edit cell"},{"cells",QJsonArray{cell}},
            {"workbook",draft["workbook"]},{"sheet",draft["sheet"]},{"expected_state",draft["state"]}});
    if (!m_pendingId.isEmpty()) {
        m_draftSubmissionId=m_pendingId;
        if (!destination.isEmpty()) { m_enterNavigation=destination; m_enterNavigation["operation_id"]=m_pendingId; }
    }
    m_frozenTop->focusCurrent();
}

bool MainWindow::resolveDraft() {
    if (!m_source->isModified()) return true;
    const auto answer = QMessageBox::question(this,tr("Resolve editor draft"),
        tr("Save or discard the draft for %1%2 before changing its target?")
            .arg(SheetModel::columnName(m_draftTarget["col"].toInt())).arg(m_draftTarget["row"].toInt()),
        QMessageBox::Save | QMessageBox::Discard | QMessageBox::Cancel, QMessageBox::Cancel);
    if (answer == QMessageBox::Discard) { m_source->setModified(false); clearRecoveredDraft(); return true; }
    if (answer == QMessageBox::Save) submitDraft();
    return false;
}

void MainWindow::clearRecoveredDraft() {
    m_recoveredDraft={}; m_restoreDraft=false; m_draftNeedsReview=false; m_draftSubmissionId.clear();
}

void MainWindow::captureGridDraft(bool needsReview) {
    if (!m_pendingId.isEmpty() || m_source->isModified()) return;
    auto *delegate=dynamic_cast<CellDelegate *>(m_grid->itemDelegate());
    auto draft=delegate?delegate->takeDraft():QJsonObject{};
    for(auto *pane:m_frozenTop->views())if(draft.isEmpty())if(auto *delegate=dynamic_cast<CellDelegate *>(pane->itemDelegate()))draft=delegate->takeDraft();
    if (draft.isEmpty()) return;
    m_draftTarget={{"workbook",m_info["id"]},{"sheet",m_model->sheet()},{"state",m_info["state"]},
                   {"row",m_model->sourceRow(draft["view_row"].toInt())+1},{"col",draft["col"]},{"input_kind",draft["input_kind"]}};
    m_recoveredDraft=m_draftTarget; m_recoveredDraft["source"]=draft["source"]; m_recoveredDraft["needs_review"]=needsReview;
    m_source->setText(draft["source"].toString()); m_source->setModified(true);
    m_draftNeedsReview=needsReview;
}

void MainWindow::restoreRecoveredDraft() {
    m_restoreDraft=false;
    const auto sheet=m_recoveredDraft["sheet"].toString();
    auto retainUnavailable=[this,sheet] {
        m_draftTarget={{"workbook",m_info["id"]},{"sheet",sheet},{"state",m_info["state"]},
                       {"row",m_recoveredDraft["row"]},{"col",m_recoveredDraft["col"]},{"input_kind",m_recoveredDraft["input_kind"]}};
        m_source->setText(m_recoveredDraft["source"].toString()); m_source->setModified(true);
        m_draftNeedsReview=true;
        m_status->setText("Draft target unavailable • Copy the retained input or press Escape to discard it");
    };
    if (sheet!=m_model->sheet()) {
        retainUnavailable();
        issue("The original draft sheet is unavailable. Copy this retained draft before discarding it:\n"+m_recoveredDraft["source"].toString());
        return;
    }
    const auto target=m_model->index(m_recoveredDraft["row"].toInt()-1,m_recoveredDraft["col"].toInt()-1);
    if (!target.isValid()) { retainUnavailable(); issue("The recovered draft target is unavailable; the draft remains retained."); return; }
    if(m_hiddenRows.contains(target.row()) || m_hiddenColumns.contains(target.column())) {
        m_hiddenRows.remove(target.row()); m_hiddenColumns.remove(target.column());
        applyVisibility(); saveViewState(); issue("Shown the recovered draft's original row and column for review.");
    }
    {
        QSignalBlocker blocker(m_grid->selectionModel()); m_grid->setCurrentIndex(target); m_grid->scrollTo(target);
    }
    syncSource();
    m_draftTarget={{"workbook",m_info["id"]},{"sheet",sheet},{"state",m_info["state"]},{"row",target.row()+1},{"col",target.column()+1},
                   {"input_kind",m_recoveredDraft["input_kind"]}};
    m_source->setText(m_recoveredDraft["source"].toString()); m_source->setModified(true);
    m_draftNeedsReview=m_recoveredDraft["needs_review"].toBool();
    m_status->setText(m_draftNeedsReview ? "Recovered draft • Review against reopened input before applying" : "Rejected edit • Draft restored for correction");
}

bool MainWindow::isRecoveryPath(QString path) {
    return QDir::fromNativeSeparators(path).replace('\\','/').contains("/recovery/");
}

void MainWindow::changeSheet(int index) {
    if (m_busy || index < 0 || m_info.isEmpty()) return;
    if (!resolveDraft()) {
        QSignalBlocker blocker(m_tabs);
        for (int i=0; i<m_tabs->count(); ++i) if (m_tabs->tabData(i).toString()==m_model->sheet()) m_tabs->setCurrentIndex(i);
        return;
    }
    m_listSpec = {};
    m_model->bind(m_info["id"].toString(), m_tabs->tabData(index).toString(), m_info["state"].toString(), m_info["readonly"].toBool());
    restoreViewState();
    m_grid->setCurrentIndex(m_model->index(0, 0)); syncSource();
}

void MainWindow::changeStructure(bool columns, bool remove) {
    if (m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool()) return;
    if(!m_hiddenRows.isEmpty() || !m_hiddenColumns.isEmpty()) {issue("Show hidden rows and columns before structural edits."); return;}
    if (m_model->projected()) { issue("Clear the list view before changing rows or columns."); return; }
    const auto ranges=m_grid->selectionModel()->selection();
    if (ranges.size()!=1) { issue("Select one contiguous range of rows or columns."); return; }
    const auto range=ranges.first();
    const int start=(columns?range.left():range.top())+1;
    const int count=columns?range.width():range.height();
    auto command=targetContext({{"action",QString(columns?"columns_":"rows_")+(remove?"delete":"insert")},
                                {"start",start},{"count",count}});
    // Capture context before both draft resolution and the confirmation dialog.
    captureGridDraft(false);
    if (!resolveDraft()) return;
    if (QMessageBox::question(this,remove?tr("Delete rows or columns"):tr("Insert rows or columns"),
        tr("%1 %2 %3 starting at %4? This changes whole rows or columns, updates formula references across the workbook, and clears Undo history. Deleted references become #REF!. Required handlers and unsupported formulas can reject the entire operation.")
            .arg(remove?tr("Delete"):tr("Insert")).arg(count).arg(columns?tr("columns"):tr("rows")).arg(start),
        QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel)!=QMessageBox::Yes) return;
    submit(command);
}

void MainWindow::reportReferenceWarnings(const QJsonObject &receipt) {
    if (receipt["reference_warnings"].toInt()<=0) return;
    QStringList locations;
    for (const auto &value:receipt["reference_issues"].toArray()) {
        const auto cell=value.toObject();
        locations.append(cell["sheet"].toString()+"!"+SheetModel::columnName(cell["col"].toInt())+QString::number(cell["row"].toInt()));
    }
    issue(tr("Structure change committed: %1 reference expressions became #REF!. Affected formulas (first 100): %2")
        .arg(receipt["reference_warnings"].toInt()).arg(locations.join(", ")));
}

QJsonObject MainWindow::selectedRange() const {
    if (m_model->projected()) { const_cast<MainWindow *>(this)->issue("Clear the list view before range operations. Single-cell edits target the original source row."); return {}; }
    auto ranges = m_grid->selectionModel()->selection();
    if (ranges.isEmpty()) return {};
    const auto &range = ranges.first();
    if(intersectsHidden(range.top()+1,range.left()+1,range.bottom()+1,range.right()+1)) {
        const_cast<MainWindow *>(this)->issue("Show hidden rows and columns in this rectangle before range operations."); return {};
    }
    if (qint64(range.height())*range.width() > 10000) {
        const_cast<MainWindow *>(this)->issue("Selection exceeds the 10,000-cell ordinary operation limit."); return {};
    }
    return {{"r1", range.top()+1}, {"c1", range.left()+1}, {"r2", range.bottom()+1}, {"c2", range.right()+1}};
}

QJsonObject MainWindow::targetContext(QJsonObject arguments) const {
    arguments["workbook"]=m_info["id"]; arguments["sheet"]=m_model->sheet(); arguments["expected_state"]=m_info["state"];
    return arguments;
}

bool MainWindow::matchesTarget(const QJsonObject &target) const {
    return target["workbook"]==m_info["id"] && target["sheet"].toString()==m_model->sheet()
        && target["expected_state"]==m_info["state"];
}

void MainWindow::clearCells(const QString &mode) {
    if(m_busy || m_unknown || m_info.isEmpty()) return;
    if(m_info["readonly"].toBool()) {issue("Workbook is read-only. Clearing is unavailable.");return;}
    if(!QStringList{"contents","formats","all"}.contains(mode)) {issue("Unknown clear action.");return;}
    const auto range=selectedRange(); if(range.isEmpty()) return;
    auto command=targetContext(range); QJsonArray cells;
    for(int row=range["r1"].toInt();row<=range["r2"].toInt();++row)
        for(int col=range["c1"].toInt();col<=range["c2"].toInt();++col) {
            QJsonObject item{{"row",row},{"col",col}};
            if(mode=="all") item["clear"]=true;
            else if(mode=="formats") {item["style"]=QJsonObject{};item["replace_style"]=true;}
            else item["clear_input"]=true;
            cells.append(item);
        }
    command["action"]="edit";command["cells"]=cells;
    command["label"]=mode=="all"?tr("Clear all"):mode=="formats"?tr("Clear formatting"):tr("Clear contents");
    submit(command);
}

void MainWindow::format(const QJsonObject &style, QJsonObject target) {
    if (m_busy) return;
    if (target.isEmpty()) {
        target=selectedRange(); if (target.isEmpty()) return;
        target=targetContext(target);
    }
    QJsonArray edits;
    for (int row = target["r1"].toInt(); row <= target["r2"].toInt(); ++row)
        for (int col = target["c1"].toInt(); col <= target["c2"].toInt(); ++col)
            edits.append(QJsonObject{{"row", row}, {"col", col}, {"style", style}});
    target["action"]="edit"; target["label"]="Format cells"; target["cells"]=edits; submit(target);
}

void MainWindow::saveCopy(bool firstSave) {
    if (m_busy || m_info.isEmpty()) return;
    if (m_source->isModified()) { issue("Submit or cancel the formula-bar draft before saving a copy."); return; }
    auto path = QFileDialog::getSaveFileName(this, tr("Save native copy"), {}, tr("DataHorizon (*.dhwb)"));
    if (path.isEmpty()) return;
    if (!path.endsWith(".dhwb", Qt::CaseInsensitive)) path += ".dhwb";
    setBusy(true, "Creating consistent native snapshot…");
    m_client->request(base()+"/snapshot", {{"path", path}}, [this, path, firstSave](QJsonObject, QString error) {
        setBusy(false, error.isEmpty() ? "Native copy stored" : "Copy failed • Source retained");
        if (!error.isEmpty()) issue(error);
        else if (firstSave) openBook(path);
    });
}

void MainWindow::save(bool copy) {
    if (m_unknown || m_info.isEmpty()) return;
    m_saveMode = copy ? 2 : 1;
    if (m_busy) { if (m_pendingId.isEmpty()) m_saveMode=0; return; }
    captureGridDraft(false);
    if (m_source->isModified()) { m_source->returnPressed(); return; }
    m_saveMode=0;
    if (copy || isRecoveryPath(m_info["path"].toString())) saveCopy(!copy);
    else m_status->setText("Stored automatically • Save confirmed");
}

void MainWindow::copy(bool cut) {
    if (m_busy) return;
    const auto generation=++m_clipboardGeneration;
    if (m_source->hasFocus()) { if (cut) m_source->cut(); else m_source->copy(); return; }
    auto range = selectedRange(); if (range.isEmpty()) return;
    range["sheet"] = m_model->sheet();
    const auto sourceWorkbook = m_info["id"].toString();
    m_client->request(base()+"/clipboard", range, [this, range, cut, sourceWorkbook, generation](QJsonObject result, QString error) {
        if (generation!=m_clipboardGeneration) return;
        if (!error.isEmpty()) { issue(error); return; }
        QHash<quint64, QJsonObject> cells;
        for (const auto &cell : result["cells"].toArray()) { auto c = cell.toObject(); cells.insert((quint64(c["row"].toInt()) << 32) | quint32(c["col"].toInt()), c); }
        QJsonArray rows; QStringList lines;
        for (int r = range["r1"].toInt(); r <= range["r2"].toInt(); ++r) {
            QJsonArray row; QStringList values;
            for (int c = range["c1"].toInt(); c <= range["c2"].toInt(); ++c) {
                auto cell = cells.value((quint64(r)<<32)|quint32(c)); row.append(cell); values.append(cell["display"].toString());
            }
            rows.append(row); lines.append(values.join('\t'));
        }
        auto *mime = new QMimeData;
        mime->setText(lines.join('\n'));
        QJsonObject payload{{"rows", rows}, {"origin_row", range["r1"]}, {"origin_col", range["c1"]},
                            {"r1",range["r1"]},{"c1",range["c1"]},{"r2",range["r2"]},{"c2",range["c2"]},
                            {"cut",cut},{"cut_state",result["state"]},{"source_workbook",sourceWorkbook},{"source_sheet",range["sheet"]}};
        mime->setData("application/x-datahorizon-cells", QJsonDocument(payload).toJson(QJsonDocument::Compact));
        QApplication::clipboard()->setMimeData(mime);
        if(cut) m_status->setText("Cut pending • Source retained until a managed paste commits • References follow moved cells");
    });
}

void MainWindow::paste(bool valuesOnly, bool formatsOnly) {
    if (m_busy) return;
    if (m_model->projected()) { issue("Paste into a sorted or filtered projection is unavailable. Clear the list view first."); return; }
    if (m_source->hasFocus()) { m_source->paste(); return; }
    const auto *mime = QApplication::clipboard()->mimeData();
    if (!mime || !m_grid->currentIndex().isValid()) return;
    QJsonObject command;
    constexpr int maxBytes=4*1024*1024, maxCells=10000;
    if (mime->hasFormat("application/x-datahorizon-cells")) {
        const auto bytes=mime->data("application/x-datahorizon-cells");
        if (bytes.size()>maxBytes) { issue("Clipboard exceeds the 4 MiB limit. Copy a smaller selection."); return; }
        QJsonParseError error;
        const auto document=QJsonDocument::fromJson(bytes,&error);
        if (error.error!=QJsonParseError::NoError || !document.isObject()) { issue("Invalid native clipboard data."); return; }
        command=document.object();
    }
    else {
        QJsonArray rows;
        auto text = mime->text();
        if (text.size()>maxBytes || text.toUtf8().size()>maxBytes) { issue("Clipboard exceeds the 4 MiB limit. Copy a smaller selection."); return; }
        if (text.endsWith('\n')) text.chop(1);
        int cells=1;
        for (auto character:text) {
            if ((character=='\t' || character=='\n') && ++cells>maxCells) { issue("Clipboard exceeds the 10,000-cell limit. Copy a smaller selection."); return; }
        }
        for (auto line : text.split('\n')) {
            if (line.endsWith('\r')) line.chop(1);
            QJsonArray row;
            for (auto value : line.split('\t')) row.append(QJsonObject{{"source", value}, {"input_kind", "auto"}});
            rows.append(row);
        }
        command["rows"] = rows;
    }
    if (command.contains("rows") || !command["cut"].toBool()) {
        if (!command["rows"].isArray()) { issue("Invalid native clipboard rows."); return; }
        int cells=0;
        const auto rows=command["rows"].toArray();
        if (rows.size()>maxCells) { issue("Clipboard exceeds the 10,000-cell limit. Copy a smaller selection."); return; }
        for (const auto &row:rows) {
            if (!row.isArray()) { issue("Invalid native clipboard rows."); return; }
            cells+=row.toArray().size();
            if (cells>maxCells) { issue("Clipboard exceeds the 10,000-cell limit. Copy a smaller selection."); return; }
        }
    }
    if(command["cut"].toBool()) {
        if (valuesOnly || formatsOnly) { issue("Cut supports full paste only. Use Copy for values-only or formats-only paste."); return; }
        const qint64 rows=qint64(command["r2"].toInt())-command["r1"].toInt()+1;
        const qint64 cols=qint64(command["c2"].toInt())-command["c1"].toInt()+1;
        if (rows>maxCells || cols>maxCells || (rows>0 && cols>0 && rows*cols>maxCells)) { issue("Clipboard exceeds the 10,000-cell limit. Copy a smaller selection."); return; }
        if(command["source_workbook"]!=m_info["id"] || command["source_sheet"].toString()!=m_model->sheet()) { issue("Cut currently supports a move within the same sheet. Use Copy for other sheets or workbooks."); return; }
        command["action"]="move"; command["label"]="Move cells";
    } else { command["action"] = "paste"; command["label"] = "Paste cells"; }
    command["row"] = m_grid->currentIndex().row()+1; command["col"] = m_grid->currentIndex().column()+1;
    int rows=0,cols=0;
    if(command["cut"].toBool()) {
        rows=command["r2"].toInt()-command["r1"].toInt()+1; cols=command["c2"].toInt()-command["c1"].toInt()+1;
        if(intersectsHidden(command["r1"].toInt(),command["c1"].toInt(),command["r2"].toInt(),command["c2"].toInt())) {issue("Show hidden cells in the cut source before moving it."); return;}
    } else {
        const auto matrix=command["rows"].toArray(); rows=matrix.size();
        for(const auto &row:matrix) cols=qMax(cols,int(row.toArray().size()));
    }
    const int row=command["row"].toInt(),col=command["col"].toInt();
    if(intersectsHidden(row,col,row+rows-1,col+cols-1)) {issue("Show hidden rows and columns in the paste destination first."); return;}
    command["mode"] = valuesOnly ? "values" : formatsOnly ? "formats" : "all"; submit(command);
}

void MainWindow::importFile() {
    if (m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool()) return;
    if (m_model->projected()) { issue("Clear the list view before importing."); return; }
    const auto target=csvImportTarget(); if (target.isEmpty()) return;
    captureGridDraft(false);
    if (!resolveDraft()) return;
    auto validTarget=[this,target]{return matchesTarget(target) && !m_model->projected();};
    if (!validTarget()) { issue("Import target changed; import cancelled."); return; }
    const auto path=QFileDialog::getOpenFileName(this,tr("Import at selected cell"),{},
        tr("Supported files (*.csv *.tsv *.txt *.xlsx *.ods)"));
    if (path.isEmpty()) return;
    if (!validTarget()) { issue("Import target changed; import cancelled."); return; }
    const auto extension=QFileInfo(path).suffix().toLower();
    if (!QStringList{"csv","tsv","txt","xlsx","ods"}.contains(extension)) {
        issue("Unsupported import extension. Choose CSV, TSV, TXT, XLSX or ODS."); return;
    }
    constexpr int maxBytes=3*1024*1024;
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly) || file.size()>maxBytes) { issue("Import file must be readable and at most 3 MiB."); return; }
    const auto bytes=file.read(maxBytes+1);
    if (file.error()!=QFileDevice::NoError || bytes.size()>maxBytes) { issue("Import file read failed or exceeds 3 MiB."); return; }
    if (extension=="xlsx" || extension=="ods") importSpreadsheetOptions(bytes,extension,target);
    else importCsvOptions(bytes,target,extension=="tsv"?"\t":",");
}

void MainWindow::importCsvOptions(const QByteArray &bytes, QJsonObject target, const QString &defaultDelimiter) {
    QDialog dialog(this); dialog.setWindowTitle(tr("CSV import options")); dialog.setObjectName("csvOptionsDialog");
    QFormLayout layout(&dialog);
    QComboBox encoding(&dialog); encoding.setObjectName("csvEncoding");
    encoding.addItem(tr("UTF-8 (BOM accepted)"),"utf-8-sig"); encoding.addItem(tr("UTF-16 (BOM required)"),"utf-16");
    encoding.addItem(tr("Windows-1252"),"cp1252"); encoding.addItem(tr("Latin-1"),"latin-1");
    QComboBox delimiter(&dialog); delimiter.setObjectName("csvDelimiter");
    delimiter.addItem(tr("Comma"),","); delimiter.addItem(tr("Semicolon"),";");
    delimiter.addItem(tr("Tab"),"\t"); delimiter.addItem(tr("Pipe"),"|");
    delimiter.setCurrentIndex(delimiter.findData(defaultDelimiter));
    layout.addRow(tr("Encoding"),&encoding); layout.addRow(tr("Delimiter"),&delimiter);
    QLabel note(tr("Choose the file's encoding. Invalid bytes stop the import. Review parsed Text cells before applying."),&dialog);
    note.setWordWrap(true); layout.addRow(&note);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog); layout.addRow(&buttons);
    connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);
    connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted) return;
    if (!matchesTarget(target) || m_model->projected()) { issue("CSV import target changed; import cancelled."); return; }
    importCsvBytes(bytes,encoding.currentData().toString(),delimiter.currentData().toString(),target);
}

QJsonObject MainWindow::csvImportTarget() const {
    if(!m_hiddenRows.isEmpty() || !m_hiddenColumns.isEmpty()) {const_cast<MainWindow *>(this)->issue("Show hidden rows and columns before importing."); return {};}
    const auto start = m_grid->currentIndex();
    if (!start.isValid()) return {};
    const int row=m_model->sourceRow(start.row())+1, col=start.column()+1;
    const auto targetName=QFileInfo(m_info["path"].toString()).fileName()+" / "+m_tabs->tabText(m_tabs->currentIndex())
        +"!"+SheetModel::columnName(col)+QString::number(row);
    return targetContext({{"row",row},{"col",col},{"name",targetName}});
}

void MainWindow::importCsvText(const QString &text, QJsonObject target) {
    previewCsv({{"csv",text}},target);
}

void MainWindow::importCsvBytes(const QByteArray &bytes, const QString &encoding, const QString &delimiter, QJsonObject target) {
    if(bytes.size()>3*1024*1024) { issue("CSV source exceeds 3 MiB."); return; }
    previewCsv({{"csv_base64",QString::fromLatin1(bytes.toBase64())},{"encoding",encoding},{"delimiter",delimiter}},target);
}

void MainWindow::prepareImport(QJsonObject request, RestClient::Callback callback, const QString &format) {
    const auto label=format.toUpper();
    const auto id=QUuid::createUuid().toString(QUuid::WithoutBraces);
    const auto resource="/v1/jobs/"+id;
    auto *progress=new QProgressDialog(tr("Preparing %1…").arg(label),tr("Cancel"),0,0,this);
    progress->setObjectName(format+"PreparationProgress"); progress->setWindowModality(Qt::WindowModal);
    progress->setMinimumDuration(0); progress->setAutoClose(false); progress->setAutoReset(false);
    auto cancelled=std::make_shared<bool>(false);
    connect(progress,&QProgressDialog::canceled,this,[this,resource,cancelled]{
        *cancelled=true;
        m_client->request(resource+"/cancel",{},[](QJsonObject,QString){});
    });
    auto *timer=new QTimer(progress); timer->setInterval(100);
    auto finish=[this,progress,timer,resource,cancelled,callback,label](QJsonObject job,QString error) {
        if(error.isEmpty() && job["status"]=="running") {
            progress->setLabelText(tr("%1 • %2 processed").arg(job["phase"].toString()).arg(job["completed"].toInt()));
            timer->start(); return;
        }
        timer->stop(); progress->hide(); progress->deleteLater();
        if(error.isEmpty()) {
            m_client->request(resource+"/release",{},[](QJsonObject,QString){});
            if(*cancelled || job["status"]=="cancelled") error=tr("%1 preparation cancelled. No workbook changes were made.").arg(label);
            else if(job["status"]!="completed") error=job["error"].toObject()["message"].toString(tr("CSV preparation failed."));
        }
        callback(error.isEmpty()?job["result"].toObject():QJsonObject{},error);
    };
    connect(timer,&QTimer::timeout,this,[this,timer,resource,finish]{
        timer->stop(); m_client->request(resource,{},finish,false);
    });
    request["job_id"]=id;
    m_client->request("/v1/import/"+format+"/jobs",request,finish);
}

void MainWindow::previewCsv(QJsonObject request, QJsonObject target) {
    if(!m_hiddenRows.isEmpty() || !m_hiddenColumns.isEmpty()) {issue("Show hidden rows and columns before importing."); return;}
    if (m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool() || m_model->projected()) return;
    if (target.isEmpty()) target=csvImportTarget();
    if (target.isEmpty()) return;
    captureGridDraft(false);
    if (!resolveDraft()) return;
    auto validTarget=[this,target]{return matchesTarget(target) && !m_model->projected();};
    if (!validTarget()) { issue("CSV import target changed; import cancelled."); return; }
    const int row=target["row"].toInt(), col=target["col"].toInt();
    const auto targetName=target["name"].toString();
    setBusy(true,"Previewing CSV for "+targetName);
    prepareImport(request, [this,row,col,targetName,validTarget,target,request](QJsonObject preview, QString error) mutable {
        if (!error.isEmpty() || !validTarget()) { setBusy(false); issue(error.isEmpty()?"CSV import target changed; import cancelled.":error); return; }
        QStringList lines,shortLines;
        const auto rows = preview["rows"].toArray();
        if(row+rows.size()-1>1048576 || col+preview["columns"].toInt()-1>16384) {
            setBusy(false); issue("CSV destination exceeds the supported grid. Choose a different starting cell."); return;
        }
        const auto inputs=preview["inputs"].toArray();
        for (int i=0; i<qMin(8, int(rows.size())); ++i) {
            const auto fields=rows[i].toArray(); QStringList values;
            const auto typedRow=i<inputs.size()?inputs[i].toArray():QJsonArray{};
            for (int c=0;c<qMin(8,int(fields.size()));++c) {
                const auto item=c<typedRow.size()?typedRow[c].toObject():QJsonObject{};
                values.append((item.isEmpty()?fields[c].toString():item["source"].toString()).left(120));
            }
            lines.append(values.join(" | "));
            if(i<2) {
                QStringList shortValues;
                for(int c=0;c<qMin(4,int(values.size()));++c) shortValues.append(values[c].left(40));
                shortLines.append(shortValues.join(" | "));
            }
        }
        QStringList report; for(const auto &line:preview["report"].toArray()) report.append(line.toString());
        const auto conversionIssues=preview["conversion_issues"].toArray(),dateExamples=preview["date_examples"].toArray();
        for(int i=0;i<qMin(10,int(conversionIssues.size()));++i) {
            const auto cell=conversionIssues[i].toObject(); report.append(tr("CSV row %1, column %2: %3 (%4)")
                .arg(cell["row"].toInt()).arg(cell["col"].toInt()).arg(cell["message"].toString()).arg(cell["code"].toString()));
        }
        for(int i=0;i<qMin(10,int(dateExamples.size()));++i) {
            const auto cell=dateExamples[i].toObject(); report.append(tr("CSV row %1, column %2: %3 → %4")
                .arg(cell["row"].toInt()).arg(cell["col"].toInt()).arg(cell["source"].toString()).arg(cell["result"].toString()));
        }
        QMessageBox review(QMessageBox::Question,tr("Review CSV import"),
            tr("Import %1 cells at %2?").arg(preview["cells"].toInt()).arg(targetName),
            QMessageBox::Yes|QMessageBox::Cancel,this);
        // This review has a custom column-type action; keep its button identity
        // and disabled Apply state in the Qt widget on every platform.
        review.setOption(QMessageBox::Option::DontUseNativeDialog);
        review.setTextFormat(Qt::PlainText); review.setDefaultButton(QMessageBox::Cancel);
        QString summary=tr("%1 converted values; %2 conversion errors.\n%3 ambiguous slash dates use your selected date order. Dates become ISO Text.\n\nPreview (first 2 rows / 4 columns, 40 characters per field):\n%4\n\nOpen Details for encoding, column types, conversion issues and more preview rows.")
            .arg(preview["converted_cells"].toInt()).arg(preview["conversion_errors"].toInt())
            .arg(preview["ambiguous_dates"].toInt()).arg(shortLines.join('\n'));
        review.setInformativeText(summary);
        review.setDetailedText(report.join('\n')+tr("\n\nPreview (first 8 rows/columns, 120 characters per field):\n")+lines.join('\n'));
        review.button(QMessageBox::Yes)->setText(tr("Import"));
        auto *typesButton=review.addButton(tr("Column types…"),QMessageBox::ActionRole); typesButton->setObjectName("csvTypesButton");
        typesButton->setEnabled(preview["columns"].toInt()>0);
        const bool canImport=preview["can_import"].toBool(); review.button(QMessageBox::Yes)->setEnabled(canImport);
        const auto choice=review.exec();
        if (!validTarget()) { setBusy(false); issue("CSV import target changed; import cancelled."); return; }
        if(review.clickedButton()==typesButton) {
            csvTypesDialog(request,preview);
            if (!validTarget()) { setBusy(false); issue("CSV import target changed; import cancelled."); return; }
            setBusy(false); previewCsv(request,target); return;
        }
        setBusy(false);
        if (choice != QMessageBox::Yes || !canImport) return;
        QJsonArray edits;
        for (int r=0; r<rows.size(); ++r) {
            auto values = rows[r].toArray();
            const auto typedRow=r<inputs.size()?inputs[r].toArray():QJsonArray{};
            for (int c=0; c<values.size(); ++c) {
                auto item=c<typedRow.size()?typedRow[c].toObject():QJsonObject{};
                if(item.isEmpty()) item={{"source",values[c]},{"kind","text"}};
                item["row"]=row+r; item["col"]=col+c; edits.append(item);
            }
        }
        submit({{"action", "edit"}, {"label", "Import CSV"}, {"cells", edits},
                {"workbook",target["workbook"]},{"sheet",target["sheet"]},{"expected_state",target["expected_state"]}});
    });
}

bool MainWindow::csvTypesDialog(QJsonObject &request, const QJsonObject &preview) {
    QDialog dialog(this); dialog.setWindowTitle(tr("CSV column types")); dialog.setObjectName("csvTypesDialog");
    QFormLayout layout(&dialog);
    QSpinBox column(&dialog); column.setObjectName("csvTypeColumn"); column.setRange(1,preview["columns"].toInt());
    QComboBox type(&dialog); type.setObjectName("csvColumnType");
    type.addItem(tr("Text (preserve exactly)"),"text"); type.addItem(tr("Number (invariant decimal)"),"number");
    type.addItem(tr("Boolean (TRUE/FALSE)"),"boolean"); type.addItem(tr("Date YYYY-MM-DD → ISO Text"),"date_iso");
    type.addItem(tr("Date M/D/YYYY → ISO Text"),"date_mdy"); type.addItem(tr("Date D/M/YYYY → ISO Text"),"date_dmy");
    QCheckBox header(tr("First record is a header; keep it as Text"),&dialog); header.setObjectName("csvHeader");
    header.setChecked(request["header"].toBool());
    QLabel sample(&dialog); sample.setTextFormat(Qt::PlainText); sample.setWordWrap(true);
    QJsonObject types=request["column_types"].toObject();
    auto load=[&] {
        QSignalBlocker blocker(&type); const auto kind=types.value(QString::number(column.value())).toString("text");
        type.setCurrentIndex(qMax(0,type.findData(kind)));
        QStringList values; const auto rows=preview["rows"].toArray();
        for(int r=0;r<qMin(4,int(rows.size()));++r) {
            const auto fields=rows[r].toArray();
            values.append(column.value()<=fields.size()?fields[column.value()-1].toString().left(120):QString{});
        }
        sample.setText(tr("Source sample:\n%1").arg(values.join('\n')));
    };
    connect(&column,&QSpinBox::valueChanged,&dialog,[&]{load();});
    connect(&type,&QComboBox::currentIndexChanged,&dialog,[&]{
        const auto key=QString::number(column.value()),kind=type.currentData().toString();
        if(kind=="text") types.remove(key); else types[key]=kind;
    });
    layout.addRow(&header); layout.addRow(tr("CSV column (1-based)"),&column); layout.addRow(tr("Type"),&type); layout.addRow(&sample);
    QLabel note(tr("Unspecified columns remain Text. Dates become ISO Text. Empty converted fields become blank. Invalid conversions block Apply; review the result before importing."),&dialog);
    note.setWordWrap(true); layout.addRow(&note);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog); layout.addRow(&buttons);
    connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept); connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    load();
    if(dialog.exec()!=QDialog::Accepted) return false;
    request["column_types"]=types; request["header"]=header.isChecked(); return true;
}

void MainWindow::exportCsvTo(const QString &path, QJsonObject target) {
    if (m_busy || path.isEmpty()) return;
    if (target.isEmpty()) {
        target=selectedRange(); if (target.isEmpty()) return;
        target=targetContext(target);
    }
    if(intersectsHidden(target["r1"].toInt(1),target["c1"].toInt(1),target["r2"].toInt(target["r1"].toInt(1)),target["c2"].toInt(target["c1"].toInt(1)))) {issue("Show hidden cells in the export rectangle first."); return;}
    if (!matchesTarget(target)) { issue("Reviewed export inputs changed. Review the selection again."); return; }
    auto range=target; range.remove("workbook"); range.remove("expected_state");
    const auto workbookPath=m_info["path"].toString();
    if (sameFile(path,workbookPath)) { issue("CSV destination is the open native workbook. Choose a different file."); return; }
    range["sheet"] = m_model->sheet(); setBusy(true, "Exporting consistent values…");
    m_client->request(base()+"/export/csv", range, [this, path, workbookPath, target](QJsonObject result, QString error) {
        setBusy(false, error.isEmpty() ? "CSV snapshot ready" : "Export blocked");
        if (!error.isEmpty()) { issue(error); return; }
        if (!matchesTarget(target) || result["state"]!=target["expected_state"]) { issue("Reviewed export inputs changed. Export cancelled."); return; }
        if (sameFile(path,workbookPath)) { issue("CSV destination resolves to the native workbook. Choose a different file."); return; }
        QSaveFile file(path);
        auto data = result["csv"].toString().toUtf8();
        if (!file.open(QIODevice::WriteOnly) || file.write(data) != data.size() || !file.commit()) issue("CSV destination failed: " + file.errorString());
        else m_status->setText("CSV exported • Native workbook retained");
    });
}

void MainWindow::listDialog() {
    if (m_busy || !resolveDraft()) return;
    auto scope = selectedRange(); if (scope.isEmpty()) return;
    scope=targetContext(scope);
    QDialog dialog(this); dialog.setWindowTitle(tr("Declared list scope"));
    auto *form = new QFormLayout(&dialog);
    form->addRow(new QLabel(tr("Source rectangle: %1%2:%3%4\nSorting changes this view; original source rows remain stable.")
        .arg(SheetModel::columnName(scope["c1"].toInt())).arg(scope["r1"].toInt())
        .arg(SheetModel::columnName(scope["c2"].toInt())).arg(scope["r2"].toInt())));
    auto *header = new QCheckBox(tr("First row is a header")); header->setChecked(true); form->addRow(header);
    auto *filterEnabled = new QCheckBox(tr("Apply comparison filter")); form->addRow(filterEnabled);
    auto *filterCol = new QSpinBox; filterCol->setRange(scope["c1"].toInt(), scope["c2"].toInt()); form->addRow(tr("Filter source column number"), filterCol);
    auto *comparison = new QComboBox; comparison->addItems({">", ">=", "=", "<>", "<", "<=", "contains"}); form->addRow(tr("Comparison"), comparison);
    auto *type = new QComboBox; type->addItems({tr("Number"), tr("Text")}); form->addRow(tr("Value type"), type);
    auto *value = new QLineEdit; form->addRow(tr("Value"), value);
    auto *sort = new QComboBox; sort->addItems({tr("Source order"), tr("Ascending"), tr("Descending")}); form->addRow(tr("Sort"), sort);
    auto *sortCol = new QSpinBox; sortCol->setRange(scope["c1"].toInt(), scope["c2"].toInt()); form->addRow(tr("Sort source column number"), sortCol);
    auto *sort2 = new QComboBox; sort2->addItems({tr("No second key"), tr("Ascending"), tr("Descending")}); form->addRow(tr("Then sort"), sort2);
    auto *sortCol2 = new QSpinBox; sortCol2->setRange(scope["c1"].toInt(), scope["c2"].toInt()); form->addRow(tr("Second source column number"), sortCol2);
    auto *buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    form->addRow(buttons); connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept); connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    if (dialog.exec() != QDialog::Accepted) return;
    QJsonArray filters, keys;
    if (filterEnabled->isChecked()) {
        QJsonValue typed = value->text();
        if (!type->currentIndex()) { bool ok; const auto number = value->text().toDouble(&ok); if (!ok || !qIsFinite(number)) { issue("Filter value is not a finite number."); return; } typed = number; }
        filters.append(QJsonObject{{"col", filterCol->value()}, {"op", comparison->currentText()}, {"value", typed}});
    }
    if (sort->currentIndex()) keys.append(QJsonObject{{"col", sortCol->value()}, {"descending", sort->currentIndex()==2}});
    if (sort2->currentIndex()) keys.append(QJsonObject{{"col", sortCol2->value()}, {"descending", sort2->currentIndex()==2}});
    scope["header"] = header->isChecked(); scope["filters"] = filters; scope["sort"] = keys;
    showList(scope);
}

void MainWindow::showList(QJsonObject scope) {
    if (m_busy || m_unknown) return;
    const auto defaults=targetContext();
    for (auto field=defaults.begin(); field!=defaults.end(); ++field)
        if (!scope.contains(field.key())) scope[field.key()]=field.value();
    if (!resolveDraft()) return;
    if (!matchesTarget(scope)) { issue("Reviewed list inputs changed. Review the scope again."); return; }
    scope.remove("workbook"); scope.remove("expected_state"); scope.remove("sheet");
    m_listSpec=scope; applyList();
}

void MainWindow::applyList() {
    if (m_listSpec.isEmpty()) return;
    const auto selected=m_grid->currentIndex();
    const int selectedRow=selected.isValid()?m_model->sourceRow(selected.row()):-1;
    const int selectedColumn=selected.isValid()?selected.column():0;
    auto request = m_listSpec; request["sheet"] = m_model->sheet(); setBusy(true, "Evaluating full list scope…");
    m_client->request(base()+"/list", request, [this,selectedRow,selectedColumn](QJsonObject result, QString error) {
        if (!error.isEmpty() || result["state"] != m_info["state"]) {
            m_listSpec = {}; setBusy(false); issue(error.isEmpty() ? "List inputs changed. Refresh before applying the view again." : error); emit workbookReady(); return;
        }
        QList<int> rows; for (const auto &row : result["rows"].toArray()) rows.append(row.toInt()-1);
        m_model->setProjection(rows);
        restoreViewState();
        if (!rows.isEmpty()) m_grid->setCurrentIndex(m_model->index(qMax(0,m_model->viewRow(selectedRow)),selectedColumn));
        setBusy(false, tr("List view • %1 / %2 records • Edits target original source rows • Clear list view to restore sheet")
            .arg(result["matching"].toInt()).arg(result["total"].toInt()));
        applyEnterNavigation();
        syncSource(); emit workbookReady();
    });
}

void MainWindow::queueSummary() {
    if(!m_summary || !m_summaryTimer)return;
    QJsonObject key;QString reason;
    if(m_busy || m_unknown || m_info.isEmpty())reason=tr("Summary unavailable while workbook state is changing.");
    else if(m_model->projected())reason=tr("Clear the list view to summarize one source rectangle.");
    else {
        const auto ranges=m_grid->selectionModel()->selection();
        if(ranges.size()!=1)reason=tr("Select one rectangle to summarize stored cells.");
        else {
            const auto &range=ranges.first();
            if(qint64(range.height())*range.width()>10000)reason=tr("Summary selection exceeds 10,000 cells.");
            else if(intersectsHidden(range.top()+1,range.left()+1,range.bottom()+1,range.right()+1))reason=tr("Show hidden cells in this rectangle before summarizing it.");
            else key=targetContext({{"r1",range.top()+1},{"c1",range.left()+1},{"r2",range.bottom()+1},{"c2",range.right()+1}});
        }
    }
    if(!key.isEmpty() && key==m_summaryKey)return;
    ++m_summaryGeneration;m_summaryTimer->stop();m_summaryKey=key;
    if(key.isEmpty()) {m_summary->setText(tr("Summary unavailable"));m_summary->setToolTip(reason);return;}
    m_summary->setText(tr("Summary…"));m_summary->setToolTip(tr("Reading stored selection; unsubmitted drafts are excluded."));m_summaryTimer->start();
}

void MainWindow::requestSummary() {
    const auto key=m_summaryKey;const auto generation=m_summaryGeneration;
    if(key.isEmpty() || m_busy || m_unknown || m_summaryInFlight || !matchesTarget(key))return;
    m_summaryInFlight=true;const auto flight=++m_summaryFlight;
    auto request=key;request.remove("workbook");request.remove("expected_state");
    m_client->request(base()+"/summary",request,[this,key,generation,flight](QJsonObject result,QString error){
        if(flight!=m_summaryFlight)return;
        m_summaryInFlight=false;
        if(generation!=m_summaryGeneration || key!=m_summaryKey || m_busy || m_unknown || !matchesTarget(key)) {
            if(!m_summaryKey.isEmpty() && !m_busy && !m_unknown)m_summaryTimer->start();
            return;
        }
        if(!error.isEmpty()) {m_summary->setText(tr("Summary unavailable"));m_summary->setToolTip(error);return;}
        if(!result["state"].isString()) {
            m_summary->setText(tr("Summary unavailable"));m_summary->setToolTip(tr("Invalid selection summary response."));return;
        }
        if(result["state"]!=key["expected_state"]) {
            m_summary->setText(tr("Summary unavailable"));m_summary->setToolTip(tr("Stored data changed. Refreshing the workbook."));
            m_model->externalChange();return;
        }
        const QJsonArray rectangle{key["r1"],key["c1"],key["r2"],key["c2"]};
        const bool unavailable=result["error_count"].toInt()>0 || result["numeric_error"].isString();
        if((!unavailable && (!result["sum"].isDouble() || (result["numeric_count"].toInt()>0 && !result["average"].isDouble())))
            || result["sheet"]!=key["sheet"] || result["range"].toArray()!=rectangle || !result["count"].isDouble()
            || !result["numeric_count"].isDouble() || !result["selected_cells"].isDouble() || !result["error_count"].isDouble()
            || !(result["sum"].isDouble() || result["sum"].isNull()) || !(result["average"].isDouble() || result["average"].isNull())
            || !(result["numeric_error"].isString() || result["numeric_error"].isNull())) {
            m_summary->setText(tr("Summary unavailable"));m_summary->setToolTip(tr("Invalid selection summary response."));return;
        }
        QString text;
        if(result["error_count"].toInt()>0 || !result["numeric_error"].isNull())
            text=tr("Count %1 • Sum/average unavailable").arg(result["count"].toInt());
        else text=tr("Count %1 • Sum %2 • Average %3").arg(result["count"].toInt())
            .arg(QString::number(result["sum"].toDouble(),'g',12))
            .arg(result["average"].isNull()?tr("—"):QString::number(result["average"].toDouble(),'g',12));
        m_summary->setText(text);
        m_summary->setToolTip(text+tr("\n%1 selected cells; %2 numeric values; %3 formula errors. Stored values, excluding unsubmitted drafts; Text/Booleans excluded from numeric summary. Percentage fractions stay unscaled.")
            .arg(result["selected_cells"].toInt()).arg(result["numeric_count"].toInt()).arg(result["error_count"].toInt())
            +(result["numeric_error"].isString()?tr("\nNumeric subtotal overflow (#NUM!)."):QString()));
    });
}

void MainWindow::clearFindResults() {
    m_findScope={};m_findMatches={};m_findIndex=-1;m_findTotal=0;
    if(m_findNext)m_findNext->setEnabled(false);
    if(m_findPrevious)m_findPrevious->setEnabled(false);
}

void MainWindow::navigateFind(int direction) {
    if(m_busy || m_unknown || m_findMatches.isEmpty())return;
    if(m_model->projected() || !matchesTarget(m_findScope)) {clearFindResults();issue("Find results expired. Search the selection again.");return;}
    const auto scope=m_findScope;captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || m_unknown || !matchesTarget(scope) || scope!=m_findScope) {clearFindResults();return;}
    const int count=m_findMatches.size();
    const int next=m_findIndex<0?(direction<0?count-1:0):(m_findIndex+direction+count)%count;
    const auto match=m_findMatches[next].toObject();const int row=match["row"].toInt(),col=match["col"].toInt();
    if(intersectsHidden(row,col,row,col)) {issue("The next match is hidden. Show its row/column before navigating to it.");return;}
    const auto index=m_model->index(row-1,col-1);m_grid->setCurrentIndex(index);
    m_grid->selectionModel()->select(index,QItemSelectionModel::ClearAndSelect);
    if(m_grid->currentIndex()!=index)return;
    m_findIndex=next;
    m_frozenTop->focusCurrent();
    m_status->setText(m_findTotal>count?tr("Find %1 of first %2 / %3 matches • Narrow the search for remaining matches").arg(next+1).arg(count).arg(m_findTotal):tr("Find %1 of %2 matches • Wraparound within searched selection").arg(next+1).arg(count));
}

void MainWindow::findDialog(bool replace) {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    auto scope = selectedRange(); if (scope.isEmpty()) return;
    scope=targetContext(scope);
    const auto workbook=m_info["id"].toString();
    captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(scope)) {issue("Search target changed. Review the scope again.");return;}
    bool ok;
    auto needle = QInputDialog::getText(this, tr("Find in selected rectangle"), tr("Case-sensitive text"), QLineEdit::Normal, {}, &ok);
    if (!ok || needle.isEmpty()) return;
    auto field = QInputDialog::getItem(this, tr("Search field"), tr("Search"), {tr("Stored input / formula source"), tr("Displayed value")}, 0, false, &ok);
    if (!ok) return;
    if (m_busy || workbook!=m_info["id"].toString() || scope["expected_state"]!=m_info["state"]
        || scope["sheet"].toString()!=m_model->sheet()) { issue("Search inputs changed. Refresh and review the scope again."); return; }
    scope["needle"] = needle; scope["field"] = field == tr("Displayed value") ? "display" : "source";
    setBusy(true, "Finding within declared scope…");
    auto request=scope; request.remove("workbook");
    m_client->request(base()+"/find", request, [this, scope, replace, workbook](QJsonObject result, QString error) mutable {
        if (m_info["id"].toString()!=workbook) return;
        setBusy(false);
        if (!error.isEmpty()) { issue(error); return; }
        if (result["state"] != scope["expected_state"] || result["state"]!=m_info["state"]
            || scope["sheet"].toString()!=m_model->sheet()) { issue("Search inputs changed. Refresh and review the scope again."); return; }
        QStringList locations; for (const auto &value : result["matches"].toArray()) locations.append(value.toObject()["address"].toString());
        issue(tr("%1 matching cells: %2%3").arg(result["count"].toInt()).arg(locations.join(", ")).arg(result["truncated"].toBool() ? " (first 500 shown)" : ""));
        if(!replace) {
            m_findScope=scope;m_findMatches=result["matches"].toArray();m_findTotal=result["count"].toInt();m_findIndex=-1;
            m_findNext->setEnabled(!m_findMatches.isEmpty());m_findPrevious->setEnabled(!m_findMatches.isEmpty());
            if(!m_findMatches.isEmpty())navigateFind(1);
            return;
        }
        if(result["count"].toInt()==0)return;
        bool ok; auto replacement = QInputDialog::getText(this, tr("Replace all"), tr("Replacement text"), QLineEdit::Normal, {}, &ok);
        if (!ok) return;
        if (QMessageBox::question(this, tr("Review Replace All"), tr("Replace text in %1 matching cells as one atomic operation? Required rules can reject the entire change. Formula result replacement is blocked; use formula source to edit a formula.").arg(result["count"].toInt())) != QMessageBox::Yes) return;
        scope["replacement"] = replacement; scope["action"] = "replace_all"; scope["label"] = "Replace all"; submit(scope);
    });
}

void MainWindow::closeEvent(QCloseEvent *event) {
    if (m_unknown && !m_pendingId.isEmpty()) {
        if (!preserveUnknownOperation()) { issue("Keep the workbook path and operation ID before exiting: "+m_pendingId); event->ignore(); return; }
        const auto answer=QMessageBox::warning(this,tr("Close with an unknown operation outcome?"),
            tr("The edit may have committed. Closing does not discard stored data.\n\nWorkbook: %1\nOperation: %2\nRecovery record: %3\n\nReopen and look up this ID before retrying the edit.")
                .arg(m_info["path"].toString(),m_pendingId,m_recoveryRecord),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel);
        if (answer==QMessageBox::Yes) { event->accept(); return; }
        event->ignore(); return;
    }
    if (!m_pendingId.isEmpty() || m_unknown || m_releasing) {
        issue("Wait for a known operation outcome before closing. Committed work remains in the native workbook."); event->ignore(); return;
    }
    captureGridDraft(false);
    if (m_source->isModified() || !m_recoveredDraft.isEmpty()) {
        if (QMessageBox::question(this, tr("Discard editor draft?"), tr("Discard the unsubmitted formula-bar draft? Already stored edits remain in the workbook.")) != QMessageBox::Yes) { event->ignore(); return; }
        m_source->setModified(false); clearRecoveredDraft();
    }
    if (!m_options.url.isEmpty() && !m_info.isEmpty() && !m_released) {
        const auto workbook=m_info["id"].toString(), service=m_client->url();
        m_releasing=true; setBusy(true,"Closing workbook…"); event->ignore();
        m_client->request(base()+"/close",{},[this,workbook,service](QJsonObject,QString){
            if (!m_releasing) return;
            m_releasing=false;
            if (m_info["id"].toString()!=workbook || m_client->url()!=service) { setBusy(false,"Current workbook remains open"); return; }
            m_released=true; close();
        });
        return;
    }
    event->accept();
}

bool MainWindow::eventFilter(QObject *object, QEvent *event) {
    if (object == m_source && event->type() == QEvent::KeyPress) {
        const auto *key=static_cast<QKeyEvent *>(event);
        if (key->key()==Qt::Key_Return || key->key()==Qt::Key_Enter) {
            submitDraft(true,key->modifiers().testFlag(Qt::ShiftModifier)?-1:1); return true;
        }
        if (key->key()==Qt::Key_Escape) {
            m_source->setModified(false); clearRecoveredDraft(); syncSource(); m_frozenTop->focusCurrent(); return true;
        }
    }
    return QMainWindow::eventFilter(object, event);
}

void MainWindow::importSpreadsheetOptions(const QByteArray &bytes, const QString &format, QJsonObject target) {
    QDialog dialog(this); dialog.setWindowTitle(tr("Spreadsheet import options")); dialog.setObjectName("spreadsheetOptionsDialog");
    QFormLayout layout(&dialog); QSpinBox sheet(&dialog); sheet.setRange(0,64); sheet.setValue(1);
    sheet.setSpecialValueText(tr("All sheets (empty destination)"));
    QComboBox mode(&dialog); mode.addItem(tr("Preserve supported formulas"),"formulas"); mode.addItem(tr("Cached values (freshness unverified)"),"values");
    layout.addRow(tr("Sheet number"),&sheet); layout.addRow(tr("Inputs"),&mode);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog); layout.addRow(&buttons);
    connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept); connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted || !matchesTarget(target)) return;
    importSpreadsheetBytes(bytes,format,mode.currentData().toString(),sheet.value(),target);
}

void MainWindow::importSpreadsheetBytes(const QByteArray &bytes, const QString &format, const QString &mode, int sheetIndex, QJsonObject target) {
    if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool() || m_model->projected()) return;
    if(target.isEmpty()) target=csvImportTarget();
    if(target.isEmpty()) return;
    captureGridDraft(false); if(!resolveDraft() || !matchesTarget(target)) return;
    setBusy(true,tr("Preparing %1 import…").arg(format.toUpper()));
    prepareImport({{"file_base64",QString::fromLatin1(bytes.toBase64())},{"mode",mode},{"sheet_index",sheetIndex},
                   {"scope",sheetIndex==0?"workbook":"selection"},
                   {"row",sheetIndex==0?QJsonValue(1):target["row"]},{"col",sheetIndex==0?QJsonValue(1):target["col"]}},
        [this,target,format](QJsonObject preview,QString error) {
        if(!error.isEmpty() || !matchesTarget(target) || m_model->projected()) { setBusy(false); issue(error.isEmpty()?"Import target changed; import cancelled.":error); return; }
        QStringList report; for(const auto &line:preview["report"].toArray()) report.append(line.toString());
        for(const auto &entry:preview["issues"].toArray()) { const auto item=entry.toObject(); report.append((item["sheet"].toString().isEmpty()?QString():item["sheet"].toString()+"!")+item["cell"].toString()+": "+item["message"].toString()); }
        const bool allowed=preview["can_import"].toBool();
        QMessageBox review(QMessageBox::Question,tr("Review spreadsheet import"),
            preview["scope"]=="workbook" ? tr("Import %1 sheets and %2 stored cells into this empty workbook? History will be cleared.").arg(preview["workbook_sheets"].toArray().size()).arg(preview["stored_cells"].toInt()) :
            tr("Import %1 stored cells from %2 at %3?").arg(preview["stored_cells"].toInt()).arg(preview["sheet"].toString().left(80)).arg(target["name"].toString()),
            QMessageBox::Yes|QMessageBox::Cancel,this);
        review.setObjectName("spreadsheetImportReview");
        review.setOption(QMessageBox::Option::DontUseNativeDialog); review.setTextFormat(Qt::PlainText); review.setDefaultButton(QMessageBox::Cancel);
        review.setInformativeText(allowed?tr("Review the conversion report in Details before applying. Unsupported features listed there are omitted; the source file is retained."):tr("Import blocked. Details lists unsupported or invalid cells. No cells will be applied."));
        review.setDetailedText(report.join('\n')); review.button(QMessageBox::Yes)->setEnabled(allowed);
        const auto choice=review.exec(); setBusy(false);
        if(choice!=QMessageBox::Yes || !allowed || !matchesTarget(target) || m_model->projected()) return;
        if(preview["scope"]=="workbook") {
            submit({{"action","workbook_import"},{"label","Import "+format.toUpper()+" workbook"},{"sheets",preview["workbook_sheets"]},
                    {"workbook",target["workbook"]},{"expected_state",target["expected_state"]}}); return;
        }
        submit({{"action","edit"},{"label","Import "+format.toUpper()},{"cells",preview["cells"]},
                {"workbook",target["workbook"]},{"sheet",target["sheet"]},{"expected_state",target["expected_state"]}});
    },format);
}

void MainWindow::exportFile() {
    if(m_busy || m_unknown || m_info.isEmpty()) return;
    auto range=selectedRange(); if(range.isEmpty()) return;
    auto target=targetContext(range);
    ExportFileDialog dialog(this,{tr("CSV (*.csv)"),tr("Excel workbook (*.xlsx)"),tr("OpenDocument spreadsheet (*.ods)")});
    dialog.setWindowTitle(tr("Export data"));
    if(dialog.exec()!=QDialog::Accepted || !matchesTarget(target)) return;
    const auto path=dialog.selectedFiles().value(0), format=dialog.format();
    if(path.isEmpty()) return;
    if(format=="csv") {
        if (QMessageBox::question(this,tr("CSV values export"),tr("Export selected values from one consistent input state? Formulas, styles, other sheets and native metadata are omitted. Blank, NULL and empty text all become empty fields. The native workbook is retained."),
                QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel)==QMessageBox::Yes) exportCsvTo(path,target);
        return;
    }
    bool ok=false;
    QStringList choices{tr("Inputs and supported formulas"),tr("Calculated values")};
    choices << tr("Whole workbook: inputs and supported formulas") << tr("Whole workbook: calculated values");
    const auto choice=QInputDialog::getItem(this,tr("Spreadsheet export"),tr("Export cells as"),choices,0,false,&ok);
    const auto choiceIndex=choices.indexOf(choice);
    if(choiceIndex>=2) target["scope"]="workbook";
    if(ok && matchesTarget(target)) exportSpreadsheetTo(path,format,choiceIndex%2==1?"values":"formulas",target);
}

void MainWindow::exportSpreadsheetTo(const QString &path, const QString &format, const QString &mode, QJsonObject target) {
    if(m_busy || path.isEmpty()) return;
    if(target.isEmpty()) { const auto range=selectedRange(); if(range.isEmpty()) return; target=targetContext(range); }
    if(!matchesTarget(target) || (target.value("scope").toString()!="workbook" && intersectsHidden(target["r1"].toInt(1),target["c1"].toInt(1),target["r2"].toInt(1),target["c2"].toInt(1)))) { issue("Review a visible export selection first."); return; }
    const auto native=m_info["path"].toString(); if(sameFile(path,native)) { issue("Export destination is the native workbook. Choose a different file."); return; }
    auto request=target; request.remove("workbook"); request.remove("expected_state"); request["mode"]=mode;
    setBusy(true,"Preparing spreadsheet snapshot…");
    m_client->request(base()+"/export/"+format,request,[this,path,native,target](QJsonObject result,QString error) {
        if(!error.isEmpty() || !matchesTarget(target) || result["state"]!=target["expected_state"]) { setBusy(false); issue(error.isEmpty()?"Export inputs changed; review again.":error); return; }
        QStringList report; for(const auto &line:result["report"].toArray()) report.append(line.toString());
        QMessageBox review(QMessageBox::Question,tr("Review spreadsheet export"),target.value("scope").toString()=="workbook"?tr("Save this whole-workbook snapshot?"):tr("Save this selection snapshot?"),QMessageBox::Yes|QMessageBox::Cancel,this);
        review.setOption(QMessageBox::Option::DontUseNativeDialog); review.setTextFormat(Qt::PlainText); review.setDefaultButton(QMessageBox::Cancel);
        review.setObjectName("spreadsheetExportReview");
        review.setInformativeText(report.join('\n'));
        const auto choice=review.exec(); setBusy(false);
        if(choice!=QMessageBox::Yes || !matchesTarget(target)) return;
        if(sameFile(path,native)) { issue("Export destination resolves to the native workbook. Choose another file."); return; }
        const auto data=QByteArray::fromBase64(result["file_base64"].toString().toLatin1(),QByteArray::AbortOnBase64DecodingErrors);
        if(data.isEmpty()) { issue("Invalid spreadsheet snapshot response."); return; }
        QSaveFile file(path);
        if(!file.open(QIODevice::WriteOnly) || file.write(data)!=data.size() || !file.commit()) issue("Spreadsheet destination failed: "+file.errorString());
        else m_status->setText("Spreadsheet exported • Native workbook retained");
    });
}


void MainWindow::inspectNativeFile(const QString &path) {
    if(m_busy || path.isEmpty()) return;
    setBusy(true,"Inspecting native file read-only…");
    m_client->request("/v1/recovery/inspect",{{"path",path}},[this](QJsonObject result,QString error){
        setBusy(false,error.isEmpty()?"Native inspection complete":"Native inspection failed");
        if(!error.isEmpty()) { issue(error); return; }
        QStringList report; for(const auto &line:result["report"].toArray()) report.append(line.toString());
        const auto inputs=result["inputs"].toObject();
        QMessageBox box(QMessageBox::Information,tr("Native file inspection"),
            inputs["inputs_valid"].toBool()?tr("Storage and bounded input checks passed."):tr("Storage checks passed. Input inspection found issues or reached a limit; review Details."),QMessageBox::Ok,this);
        QStringList details;
        for(const auto &entry:inputs["issues"].toArray()) {
            const auto item=entry.toObject();
            details.append(item["sheet"].toString()+"!"+QString::number(item["row"].toInt())+","+QString::number(item["col"].toInt())+": "+item["code"].toString()+" — "+item["message"].toString());
        }
        box.setDetailedText(details.join('\n'));
        box.setObjectName("nativeInspectionReview");
        box.setOption(QMessageBox::Option::DontUseNativeDialog); box.setTextFormat(Qt::PlainText);
        box.setInformativeText(result["path"].toString()+"\n"+report.join('\n')); box.exec();
    });
}
