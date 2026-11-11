// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "keyboardgrid.h"
#include "servicepaths.h"
#include "frozenpanes.h"
#include "listcriteriadialog.h"
#include "namedlistviews.h"
#include "functionhelp.h"
#include "formulaeditor.h"
#include "referencepickerdialog.h"
#include "tracenavigation.h"
#include "formatcellsdialog.h"
#include "cellborders.h"
#include "formatpainter.h"
#include <QListWidget>
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
#include <QScopeGuard>
#include <QSettings>
#include <QSaveFile>
#include <QStandardPaths>
#include <QSpinBox>
#include <QStyledItemDelegate>
#include <QSignalBlocker>
#include <QStatusBar>
#include <QTabBar>
#include <QTableView>
#include <QToolButton>
#include <QMouseEvent>
#include <QTimer>
#include <QToolBar>
#include <QUuid>
#include <QVBoxLayout>
#include "exportwriter.h"
#include "jobcloser.h"
#include "clipboarddata.h"
#include "relationalworkbench.h"
#include "pythonworkspace.h"
#include "namemanager.h"
#include <QStackedWidget>
#include <QTableWidget>
#include <algorithm>

namespace {
bool sameFile(const QString &left, const QString &right) {
    return ExportWriter::sameFile(left,right);
}

bool saveCsvSnapshot(const QString &path,const QJsonObject &result,const QString &native,QString &error) {
    const auto companion=path+".dh-types.json";
    if(sameFile(path,native) || sameFile(companion,native) || QFileInfo::exists(companion)){error="Choose another CSV path: its companion exists or a destination identifies the native workbook.";return false;}
    const auto data=result["csv"].toString().toUtf8();QSaveFile csv(path);
    if(!csv.open(QIODevice::WriteOnly) || csv.write(data)!=data.size()){error="CSV destination failed: "+csv.errorString();return false;}
    if(result.contains("temporal_schema")){
        QSaveFile types(companion);const auto metadata=QJsonDocument(result["temporal_schema"].toObject()).toJson();
        if(!types.open(QIODevice::WriteOnly) || types.write(metadata)!=metadata.size() || !types.commit()){error="CSV companion failed: "+types.errorString();return false;}
    }
    if(!csv.commit()){error="CSV commit failed; any companion was preserved for inspection: "+csv.errorString();return false;}return true;
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
    std::function<QJsonObject()> functionCatalog;
    std::function<void()> syntaxChanged;
    std::function<QList<QColor>(int,int)> referenceColors;
    std::function<bool()> canEdit;
    std::function<void(const QModelIndex &,int)> enterPressed;
    std::function<bool(const QModelIndex &,int)> tabPressed;
    bool hasEditor()const{return m_editor && m_index.isValid();}
    FormulaLineEdit *activeEditor()const{return hasEditor()?dynamic_cast<FormulaLineEdit *>(m_editor.data()):nullptr;}
    void setEditorData(QWidget *editor,const QModelIndex &index) const override {
        if(editor==m_editor && index==m_index) {
            // QAbstractItemView refreshes editor data even for display-only
            // dataChanged signals. An active editor owns its draft/caret until
            // commit, cancellation or the existing binding-reset lifecycle.
            if(m_editorInitialized)return;
            m_editorInitialized=true;
        }
        QStyledItemDelegate::setEditorData(editor,index);
    }
    void paint(QPainter *painter,const QStyleOptionViewItem &option,const QModelIndex &index) const override {
        QStyledItemDelegate::paint(painter,option,index);
        if(referenceColors){int inset=3;for(const auto &color:referenceColors(index.row(),index.column())){painter->save();painter->setClipRect(option.rect);painter->setBrush(Qt::NoBrush);painter->setPen(QPen(color,1.5));painter->drawRect(option.rect.adjusted(inset,inset,-inset,-inset));painter->restore();inset+=3;}}
        const auto item=index.data(SheetModel::CellRole).toJsonObject();
        paintCellBorders(painter,option.rect,item["style"].toObject(),QApplication::palette().color(QPalette::Text));
    }
    QWidget *createEditor(QWidget *parent, const QStyleOptionViewItem &option, const QModelIndex &index) const override {
        if (canEdit && !canEdit()) return nullptr;
        Q_UNUSED(option);auto *editor=new FormulaLineEdit(parent);editor->setFrame(false);editor->syntaxChanged=syntaxChanged;if(functionCatalog)editor->setCatalog(functionCatalog());
        if (auto *line = qobject_cast<QLineEdit *>(editor)) {
            line->setMaxLength(65536); m_editor=line; m_index=index;m_editorInitialized=false;
            m_original=index.data(Qt::EditRole).toString();
            const auto item=index.data(SheetModel::CellRole).toJsonObject();
            m_inputKind=item["input_kind"].toString("auto");
            editor->setFormulaAllowed(m_inputKind!="text");
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
        if(event->type()==QEvent::FocusOut)if(auto *line=dynamic_cast<FormulaLineEdit *>(object))if(line->completionVisible())return false;
        if (object==m_editor && event->type()==QEvent::KeyPress) {
            const auto *key=static_cast<QKeyEvent *>(event);
            if(auto *line=dynamic_cast<FormulaLineEdit *>(object))if(line->completionKey(key))return true;
            if((key->key()==Qt::Key_Tab || key->key()==Qt::Key_Backtab) && !(key->modifiers()&~Qt::ShiftModifier) && tabPressed
                && tabPressed(m_index,key->key()==Qt::Key_Backtab || key->modifiers().testFlag(Qt::ShiftModifier)?-1:1)) {
                QPointer<QLineEdit> editor=m_editor;emit commitData(editor);
                if(editor)emit closeEditor(editor,QAbstractItemDelegate::NoHint);return true;
            }
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
    mutable bool m_editorInitialized=false;
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
    connect(this,&MainWindow::serviceReady,this,&MainWindow::loadFunctionCatalog);
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

void MainWindow::showRelational(bool visible) {
    if(visible && (m_busy || m_unknown || m_info.isEmpty()))return;
    if(visible){captureGridDraft(false);if(!resolveDraft())return;}
    m_relationalActive=visible;m_objectStack->setCurrentIndex(visible?1:0);
    m_toolbar->setVisible(!visible);
    m_model->setSuspended(visible);
    if(!visible)m_relational->suspend();
    for(auto *menu:m_sheetMenus)menu->setEnabled(!visible);
    for(auto *action:m_sheetFileActions)action->setEnabled(!visible);
    if(visible)m_relational->bind(m_info);
    setBusy(m_busy);
}

void MainWindow::showPythonWorkspace() {
    if(m_info.isEmpty()){issue(tr("Open a workbook before opening the Python workspace."));return;}
    m_workspace->setViewStatePath(viewSettingsPath());
    m_workspace->showWorkspace();
}

void MainWindow::showNameManager() {
    if(m_info.isEmpty()){issue(tr("Open a workbook before managing names."));return;}
    if(!m_nameManager) {
        m_nameManager=new NameManagerDialog(this);connectNamesPanel(m_nameManager->panel());
        connect(m_nameManager->panel(),&NamesPanel::insertRequested,this,[this](const QString &name){
            if(m_busy || m_unknown || m_info.isEmpty())return;
            const auto found=nameByText(name);if(found.isEmpty())return;
            if(m_relationalActive){issue(tr("Return to sheets before going to a name."));return;}
            if(m_model->projected()){issue("Clear the list view before navigating by source address.");return;}
            goToName(found);});
        m_nameManager->setNames(m_names);updateNameControls();
    }
    m_nameManager->show();m_nameManager->raise();m_nameManager->activateWindow();
}

// Names are read for every accepted workbook state. A reply for an older
// state or handle is ignored, and a new handle clears the list first so names
// from another workbook are never shown.
void MainWindow::readNames(const QJsonObject &info) {
    if(info["id"].toString()!=m_namesHandle){clearNames();m_namesHandle=info["id"].toString();}
    m_namesReady=false;updateNameControls();updateAddress();
    const auto generation=++m_namesGeneration;
    m_client->request(base()+"/names",{{"expected_state",info["state"]}},[this,generation](QJsonObject result,QString error){
        if(generation!=m_namesGeneration)return;
        // A stale reply means the workbook changed outside this window: refresh
        // as other stale reads do, and the refreshed state reads names again.
        // Each stale reply asks for at most one refresh.
        if(!error.isEmpty()){
            if(result["code"]=="STALE_STATE"){m_model->externalChange();return;}
            m_namesReadFailed=true;nameMessage(tr("Names could not be read: %1. They are read again when the workbook refreshes.").arg(error));return;
        }
        if(m_namesReadFailed){m_namesReadFailed=false;nameMessage({});}
        QList<QJsonObject> sorted;for(const auto &value:result["names"].toArray())sorted.append(value.toObject());
        std::stable_sort(sorted.begin(),sorted.end(),[](const QJsonObject &a,const QJsonObject &b){return a["name"].toString().compare(b["name"].toString(),Qt::CaseInsensitive)<0;});
        QJsonArray names;for(const auto &name:sorted)names.append(name);
        m_names=names;m_namesReady=true;publishNames();
    });
}

void MainWindow::clearNames() {
    ++m_namesGeneration;m_namesHandle.clear();m_namesReady=false;
    if(!m_names.isEmpty()){m_names={};publishNames();}else updateNameControls();
}

void MainWindow::publishNames() {
    if(m_nameManager)m_nameManager->setNames(m_names);
    m_workspace->setNames(m_names);
    updateNameControls();updateAddress();
    emit namesChanged();
}

bool MainWindow::nameWritable(QString *reason) const {
    if(m_info.isEmpty())*reason=tr("Open a workbook first.");
    else if(m_info["readonly"].toBool())*reason=tr("Read-only workbook: reopen it for editing to change names.");
    else if(m_busy || m_unknown)*reason=tr("Wait for the current operation to finish.");
    else if(!m_namesReady)*reason=tr("Names are being read from the workbook.");
    else return true;
    return false;
}

void MainWindow::updateNameControls() {
    QString reason;const bool writable=nameWritable(&reason);
    if(m_nameManager)m_nameManager->panel()->setWritable(writable,reason);
    m_workspace->namesPanel()->setWritable(writable,reason);
}

void MainWindow::nameMessage(const QString &text) {
    if(m_nameManager)m_nameManager->panel()->showMessage(text);
    m_workspace->namesPanel()->showMessage(text);
}

void MainWindow::connectNamesPanel(NamesPanel *panel) {
    connect(panel,&NamesPanel::createRequested,this,[this,panel]{createName(panel->window());});
    connect(panel,&NamesPanel::editRequested,this,[this,panel](const QString &id){renameName(panel->window(),id);});
    connect(panel,&NamesPanel::retargetRequested,this,[this,panel](const QString &id){retargetName(panel->window(),id);});
    connect(panel,&NamesPanel::deleteRequested,this,[this,panel](const QString &id){deleteName(panel->window(),id);});
}

QJsonObject MainWindow::nameById(const QString &id) const {
    for(const auto &value:m_names)if(value.toObject()["id"].toString()==id)return value.toObject();
    return {};
}

QJsonObject MainWindow::nameByText(const QString &text) const {
    if(text.isEmpty())return {};
    for(const auto &value:m_names)if(value.toObject()["name"].toString().compare(text,Qt::CaseInsensitive)==0)return value.toObject();
    return {};
}

// The first name, in case-insensitive order, whose rectangle on the current
// sheet is exactly the selection.
QString MainWindow::selectionName() const {
    // Rectangles from before a pending read can be stale (a structural edit
    // moves them), so only names read for the current state are shown.
    if(!m_namesReady || m_names.isEmpty() || m_model->projected())return {};
    const auto ranges=m_grid->selectionModel()->selection();if(ranges.size()!=1)return {};
    const auto &range=ranges.first();
    for(const auto &value:m_names) {
        const auto name=value.toObject();
        if(!name["broken"].toBool() && name["sheet_id"].toString()==m_model->sheet() && name["row1"].toInt()==range.top()+1 && name["row2"].toInt()==range.bottom()+1
            && name["col1"].toInt()==range.left()+1 && name["col2"].toInt()==range.right()+1)return name["name"].toString();
    }
    return {};
}

QString MainWindow::addressText(const QModelIndex &index) const {
    const auto name=selectionName();if(!name.isEmpty())return name;
    return SheetModel::columnName(index.column()+1)+QString::number(m_model->sourceRow(index.row())+1);
}

void MainWindow::updateAddress() {
    const auto index=m_grid->currentIndex();
    if(!index.isValid() || (m_address->hasFocus() && m_address->isModified()))return;
    // A retained draft for another sheet keeps showing its own target.
    if(m_source->isModified() && m_draftTarget["sheet"].toString()!=m_model->sheet())return;
    m_address->setText(addressText(index));
}

bool MainWindow::selectionReference(QString *reference,QString *problem) const {
    if(m_relationalActive){*problem=tr("Return to sheets and select the cells to name.");return false;}
    if(m_model->projected()){*problem=tr("Clear the list view before naming cells; names refer to source cells.");return false;}
    const auto ranges=m_grid->selectionModel()->selection();
    QItemSelectionRange range;
    if(ranges.size()==1)range=ranges.first();
    else if(ranges.isEmpty() && m_grid->currentIndex().isValid())range=QItemSelectionRange(m_grid->currentIndex());
    else{*problem=tr("Select one cell or one rectangular range to name. Several separate areas cannot share a name.");return false;}
    QString sheet;for(const auto &value:m_info["sheets"].toArray())if(value.toObject()["id"].toString()==m_model->sheet())sheet=value.toObject()["name"].toString();
    if(sheet.isEmpty()){*problem=tr("The current sheet is unavailable. Refresh and try again.");return false;}
    const auto first=SheetModel::columnName(range.left()+1)+QString::number(range.top()+1),last=SheetModel::columnName(range.right()+1)+QString::number(range.bottom()+1);
    *reference="'"+QString(sheet).replace("'","''")+"'!"+(first==last?first:first+":"+last);
    return true;
}

void MainWindow::createName(QWidget *parent) {
    QString problem,reference;
    if(!nameWritable(&problem) || !selectionReference(&reference,&problem)){nameMessage(problem);return;}
    const auto target=targetContext();
    QInputDialog input(parent);input.setObjectName("newNameInput");input.setWindowTitle(tr("New name"));input.setLabelText(tr("Name for %1").arg(reference));
    if(input.exec()!=QDialog::Accepted)return;
    const auto name=input.textValue().trimmed();if(name.isEmpty())return;
    submitName(parent,target,{{"action","name_save"},{"name",name},{"reference",reference},{"label","Create name"}},
        tr("Created %1 for %2.").arg(name,reference),tr("%1 was not created").arg(name));
}

void MainWindow::renameName(QWidget *parent,const QString &id) {
    QString problem;const auto current=nameById(id);
    if(current.isEmpty())return;
    if(!nameWritable(&problem)){nameMessage(problem);return;}
    const auto target=targetContext();const auto old=current["name"].toString();
    QInputDialog input(parent);input.setObjectName("renameNameInput");input.setWindowTitle(tr("Rename name"));input.setLabelText(tr("New name for %1").arg(old));input.setTextValue(old);
    if(input.exec()!=QDialog::Accepted)return;
    const auto name=input.textValue().trimmed();if(name.isEmpty() || name==old)return;
    submitName(parent,target,{{"action","name_save"},{"name_id",id},{"name",name},{"label","Rename name"}},
        tr("Renamed %1 to %2.").arg(old,name),tr("%1 was not renamed").arg(old));
}

void MainWindow::retargetName(QWidget *parent,const QString &id) {
    QString problem,reference;const auto current=nameById(id);
    if(current.isEmpty())return;
    if(!nameWritable(&problem) || !selectionReference(&reference,&problem)){nameMessage(problem);return;}
    const auto name=current["name"].toString();
    submitName(parent,targetContext(),{{"action","name_save"},{"name_id",id},{"name",name},{"reference",reference},{"label","Retarget name"}},
        tr("%1 now refers to %2.").arg(name,reference),tr("%1 was not retargeted").arg(name));
}

void MainWindow::deleteName(QWidget *parent,const QString &id) {
    QString problem;const auto current=nameById(id);
    if(current.isEmpty())return;
    if(!nameWritable(&problem)){nameMessage(problem);return;}
    const auto target=targetContext();const auto name=current["name"].toString();
    QMessageBox box(QMessageBox::Warning,tr("Delete name?"),tr("Delete name %1? Python code that uses it stops resolving it.").arg(name),QMessageBox::Yes|QMessageBox::Cancel,parent);
    box.setObjectName("deleteNameConfirm");box.setOption(QMessageBox::Option::DontUseNativeDialog);box.setDefaultButton(QMessageBox::Cancel);
    if(box.exec()!=QMessageBox::Yes)return;
    submitName(parent,target,{{"action","name_delete"},{"name_id",id},{"label","Delete name"}},tr("Deleted %1.").arg(name),tr("%1 was not deleted").arg(name));
}

// Name changes clear Undo history: they share the workspace's session-wide
// "Don't ask again" choice, then go through submit() against the reviewed state.
void MainWindow::submitName(QWidget *parent,const QJsonObject &target,QJsonObject operation,const QString &success,const QString &failure) {
    if(!PythonWorkspace::confirmHistoryClear(parent,m_info,tr("Changing a name clears Undo history for this workbook. Continue?"),QStringLiteral("nameHistory")))return;
    QString problem;
    if(!nameWritable(&problem) || !matchesTarget(target)){nameMessage(tr("The workbook changed before the name change started. Review the names and try again."));return;}
    for(auto field=target.begin();field!=target.end();++field)operation[field.key()]=field.value();
    nameMessage({});submit(operation);
    if(m_pendingId.isEmpty()){nameMessage(tr("%1: the change did not start. Wait for the current operation to finish, then try again.").arg(failure));return;}
    m_nameOperation=m_pendingId;m_nameSuccess=success;m_nameFailure=failure;
}

void MainWindow::goToName(const QJsonObject &name) {
    if(!m_namesReady){issue(tr("Names are being read. Try again in a moment."));return;}
    const auto label=name["name"].toString();
    if(name["broken"].toBool()){issue(tr("Name %1 no longer refers to any cells").arg(label));return;}
    const int r1=name["row1"].toInt(),c1=name["col1"].toInt(),r2=name["row2"].toInt(),c2=name["col2"].toInt();
    if(qint64(r2-r1+1)*(c2-c1+1)>10000){issue(tr("Name %1 refers to more cells than the 10,000-cell selection limit.").arg(label));return;}
    const auto sheet=name["sheet_id"].toString();
    if(sheet!=m_model->sheet()) {
        int tab=-1;for(int i=0;i<m_tabs->count();++i)if(m_tabs->tabData(i).toString()==sheet)tab=i;
        if(tab<0){issue(tr("Name %1 refers to a sheet that is not shown. Refresh and try again.").arg(label));return;}
        captureGridDraft(false);if(!resolveDraft())return;
        m_tabs->setCurrentIndex(tab);
        if(m_model->sheet()!=sheet)return;
    }
    goToRange(r1,c1,r2,c2,r1,c1);
}

// Selects a source rectangle on the current sheet with the address box's
// draft, hidden-cell and size guards.
void MainWindow::goToRange(int r1,int c1,int r2,int c2,int row,int col) {
    if(qint64(r2-r1+1)*(c2-c1+1)>10000) {issue("Address range exceeds the 10,000-cell selection limit.");return;}
    if(intersectsHidden(r1,c1,r2,c2)) {issue("That address range includes hidden cells. Show hidden rows or columns before navigating there.");return;}
    const auto target=targetContext();captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(target)) {issue("Navigation target changed. Enter the address again.");return;}
    m_address->setModified(false);
    const auto current=m_model->index(row-1,col-1);
    m_grid->selectionModel()->setCurrentIndex(current,QItemSelectionModel::NoUpdate);
    m_grid->selectionModel()->select(QItemSelection(m_model->index(r1-1,c1-1),m_model->index(r2-1,c2-1)),QItemSelectionModel::ClearAndSelect);
    m_frozenTop->focusCurrent();
}

void MainWindow::createTableFromSelection() {
    if(m_busy || m_unknown || m_info.isEmpty() || m_info.value("readonly").toBool() || m_relationalActive)return;
    if(m_model->projected()){issue(tr("Clear the list view before creating a table from source coordinates."));return;}
    const auto range=selectedRange();if(range.isEmpty())return;
    const auto target=targetContext(range);captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target))return;
    QDialog options(this);options.setObjectName("tableSelectionOptions");options.setWindowTitle(tr("Create table from selection"));
    QFormLayout form(&options);QLineEdit name(m_tablePromotionDraft["name"].toString("NewTable"));name.setObjectName("selectionTableName");name.setMaxLength(64);
    QCheckBox header(tr("First selected row contains column names"));header.setObjectName("selectionTableHeader");header.setChecked(true);
    QComboBox conversion;conversion.setObjectName("selectionTableConversion");conversion.addItem(tr("Keep strict types"),"strict");conversion.addItem(tr("Convert using dot decimals and ISO dates"),"invariant");
    form.addRow(tr("Table name"),&name);form.addRow(&header);form.addRow(tr("Value conversion"),&conversion);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel);form.addRow(&buttons);
    connect(&buttons,&QDialogButtonBox::accepted,&options,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&options,&QDialog::reject);
    if(m_info["format_version"].toInt()<4){auto *copy=buttons.addButton(tr("Save native copy…"),QDialogButtonBox::ActionRole);connect(copy,&QPushButton::clicked,&options,[this,&options]{options.reject();QTimer::singleShot(0,this,[this]{saveCopy(false);});});}
    if(options.exec()!=QDialog::Accepted || !matchesTarget(target))return;
    auto request=target;request.remove("workbook");request["name"]=name.text();request["header"]=header.isChecked();request["conversion_policy"]=conversion.currentData().toString();
    if(m_tablePromotionDraft["columns"].toArray().size()==range["c2"].toInt()-range["c1"].toInt()+1)request["columns"]=m_tablePromotionDraft["columns"];
    setBusy(true,tr("Preparing table values…"));
    m_client->request(base()+"/tables/preview",request,[this,target,request](QJsonObject preview,QString error)mutable{
        if(!error.isEmpty() || !matchesTarget(target)){setBusy(false);issue(error.isEmpty()?tr("Source selection changed. Review it again."):error);return;}
        QJsonObject definition{{"name",request["name"]},{"columns",preview["columns"]}};
        if(!RelationalWorkbench::editDefinition(this,definition,tr("Review table columns"),true) || !matchesTarget(target)){setBusy(false);return;}
        m_tablePromotionDraft=definition;request["name"]=definition["name"];request["columns"]=definition["columns"];request["preview_clock"]=preview["preview_clock"];
        m_client->request(base()+"/tables/preview",request,[this,target,request](QJsonObject result,QString failure){
            if(!failure.isEmpty() || !matchesTarget(target)){setBusy(false);issue(failure.isEmpty()?tr("Table review is stale; prepare it again."):failure);return;}
            QDialog review(this);review.setObjectName("tablePromotionReview");review.setWindowTitle(tr("Review independent table creation"));review.resize(700,450);QVBoxLayout layout(&review);
            QLabel summary(tr("Create %1 with %2 records? Formula results become values. The source selection is retained; session Undo/Redo clears.").arg(request["name"].toString()).arg(result["record_count"].toInt()));summary.setWordWrap(true);layout.addWidget(&summary);
            const auto cols=result["columns"].toArray(),rows=result["records_preview"].toArray();
            QTableWidget values(rows.size(),cols.size());values.setObjectName("tablePromotionValues");values.setAccessibleName(tr("Reviewed table values preview"));values.setEditTriggers(QAbstractItemView::NoEditTriggers);
            QStringList labels;for(const auto &col:cols)labels.append(col.toObject()["name"].toString());values.setHorizontalHeaderLabels(labels);
            for(int r=0;r<rows.size();++r)for(int c=0;c<cols.size();++c){const auto value=rows[r].toObject()[cols[c].toObject()["id"].toString()];const auto text=value.isNull()?tr("NULL"):value.toVariant().toString();values.setItem(r,c,new QTableWidgetItem(text.left(256)+(text.size()>256?QString("…"):QString{})));}
            layout.addWidget(&values,1);QStringList report;for(const auto &line:result["report"].toArray())report.append(line.toString());for(const auto &entry:result["issues"].toArray()){const auto item=entry.toObject();report.append(tr("Row %1, %2: %3").arg(item["row"].toInt()).arg(item["column"].toString(),item["message"].toString()));}
            QLabel details(report.join('\n'));details.setWordWrap(true);layout.addWidget(&details);
            QDialogButtonBox confirm(QDialogButtonBox::Apply|QDialogButtonBox::Cancel);confirm.button(QDialogButtonBox::Apply)->setEnabled(result["can_create"].toBool());layout.addWidget(&confirm);
            connect(confirm.button(QDialogButtonBox::Apply),&QPushButton::clicked,&review,&QDialog::accept);connect(&confirm,&QDialogButtonBox::rejected,&review,&QDialog::reject);
            const auto answer=review.exec();setBusy(false);
            if(answer!=QDialog::Accepted || !result["can_create"].toBool() || !matchesTarget(target))return;
            auto operation=request;operation["workbook"]=target["workbook"];operation["action"]="table_promote";operation["columns"]=result["columns"];operation["review_digest"]=result["review_digest"];operation["preview_clock"]=result["preview_clock"];submit(operation);
        });
    });
}

void MainWindow::buildUi() {
    auto *file = menuBar()->addMenu(tr("&File"));
    auto *edit = menuBar()->addMenu(tr("&Edit"));
    auto *formatMenu = menuBar()->addMenu(tr("F&ormat"));
    auto *sheetMenu = menuBar()->addMenu(tr("&Sheet"));
    auto *dataMenu = menuBar()->addMenu(tr("&Data"));
    auto *localized=dataMenu->addAction(tr("Enter localized value…"));localized->setObjectName("enterLocalizedAction");connect(localized,&QAction::triggered,this,&MainWindow::enterLocalized);
    auto *declare=dataMenu->addAction(tr("Declare selected list records…"));declare->setObjectName("declareRecordsAction");connect(declare,&QAction::triggered,this,&MainWindow::declareRecords);
    auto *records=dataMenu->addAction(tr("Work with declared records…"));records->setObjectName("recordCommandAction");connect(records,&QAction::triggered,this,&MainWindow::recordCommand);
    auto *jobs=dataMenu->addAction(tr("Operation jobs…"));jobs->setObjectName("operationJobsAction");connect(jobs,&QAction::triggered,this,&MainWindow::operationJobs);
    auto *calculate=dataMenu->addAction(tr("Recalculate selection in background…"));calculate->setObjectName("calculateSelectionAction");connect(calculate,&QAction::triggered,this,&MainWindow::calculateSelection);
    // Tools stays enabled while the relational workbench is shown so the
    // Python workspace can be opened whenever a workbook is open.
    auto *tools=menuBar()->addMenu(tr("&Tools"));
    auto *workspace=tools->addAction(tr("Python workspace"));workspace->setObjectName("pythonWorkspaceAction");connect(workspace,&QAction::triggered,this,&MainWindow::showPythonWorkspace);
    auto *view = menuBar()->addMenu(tr("&View"));
    auto *savePortable=view->addAction(tr("Save view in workbook"));savePortable->setObjectName("savePortableViewAction");connect(savePortable,&QAction::triggered,this,[this]{portableView(true);});
    auto *loadPortable=view->addAction(tr("Load view from workbook"));loadPortable->setObjectName("loadPortableViewAction");connect(loadPortable,&QAction::triggered,this,[this]{portableView(false);});
    auto *help = menuBar()->addMenu(tr("&Help"));
    m_sheetMenus={edit,formatMenu,sheetMenu,dataMenu,view};
    auto *tables=dataMenu->addAction(tr("Tables and queries…"));tables->setObjectName("relationalWorkbenchAction");
    connect(tables,&QAction::triggered,this,[this]{showRelational(true);});
    auto *promote=dataMenu->addAction(tr("Create table from selection…"));promote->setObjectName("tableFromSelectionAction");
    connect(promote,&QAction::triggered,this,&MainWindow::createTableFromSelection);
    auto *nameManager=dataMenu->addAction(tr("Name manager…"));nameManager->setObjectName("nameManagerAction");
    connect(nameManager,&QAction::triggered,this,&MainWindow::showNameManager);
    auto *functionHelp=help->addAction(tr("Formula functions…"));functionHelp->setObjectName("functionHelpAction");connect(functionHelp,&QAction::triggered,this,&MainWindow::showFunctionHelp);
    connect(m_client,&RestClient::configured,this,[this]{m_functionCatalog={};for(auto *line:findChildren<QLineEdit *>())if(auto *formula=dynamic_cast<FormulaLineEdit *>(line))formula->setCatalog({});});
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
    auto *importAction=add(file, tr("Import…"), {}, [this] { importFile(); });importAction->setObjectName("importAction");m_sheetFileActions.append(importAction);
    auto *exportAction=add(file, tr("Export…"), {}, [this] { exportFile(); });exportAction->setObjectName("exportAction");m_sheetFileActions.append(exportAction);
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
    add(file,tr("Open for relationship repair…"),{},[this]{if((m_busy && !m_refreshFailed) || m_unknown)return;const auto path=QFileDialog::getOpenFileName(this,tr("Open native workbook for restricted relationship repair"),{},tr("DataHorizon (*.dhwb)"));if(!path.isEmpty())openBook(path,false,false,true);});
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
    add(edit,tr("Trace precedents…"),{},[this]{traceFormula("precedents");})->setObjectName("tracePrecedentsAction");
    add(edit,tr("Trace dependents…"),{},[this]{traceFormula("dependents");})->setObjectName("traceDependentsAction");
    clearFindResults();
    connect(m_model,&QAbstractItemModel::modelReset,this,&MainWindow::clearFindResults);
    connect(m_client,&RestClient::configured,this,&MainWindow::clearFindResults);
    add(edit, tr("Replace all in selection…"), QKeySequence::Replace, [this] { findDialog(true); });
    add(edit, tr("Reconcile unknown operation"), {}, [this] { reconcile(); });
    m_painterAction=add(formatMenu,tr("Format painter"),{},[this]{startFormatPainter();},true);m_painterAction->setObjectName("formatPainterAction");m_painterAction->setCheckable(true);m_painterAction->setToolTip(tr("Copy the selected style pattern, then click a destination or drag an exact multiple. Escape cancels."));
    m_painterCancel=new QAction(this);m_painterCancel->setShortcut(QKeySequence(Qt::Key_Escape));m_painterCancel->setShortcutContext(Qt::WidgetWithChildrenShortcut);m_painterCancel->setEnabled(false);addAction(m_painterCancel);connect(m_painterCancel,&QAction::triggered,this,[this]{cancelFormatPainter();m_status->setText(tr("Format painter cancelled"));});
    add(formatMenu,tr("Format Cells…"),QKeySequence("Ctrl+1"),[this]{showFormatCells();})->setObjectName("formatCellsAction");
    add(edit,tr("Enter date/time…"),{},[this]{enterTemporal();})->setObjectName("enterTemporalAction");
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
    for(const int change:{-1,1}){auto *action=add(formatMenu,change>0?tr("More decimal places"):tr("Fewer decimal places"),{},[this,change]{adjustDecimals(change);},true);action->setObjectName(change>0?"moreDecimalsAction":"fewerDecimalsAction");action->setToolTip(tr("Use the active cell's precision, adjusted by one (0–15), for the selection. Percentage categories stay percentages."));}
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
    add(dataMenu, tr("Sort / filter selected list…"), {}, [this] { listDialog(); }, true)->setObjectName("listCriteriaAction");
    add(dataMenu,tr("Save named list view…"),{},[this]{namedListDialog(0);})->setObjectName("saveNamedListViewAction");
    add(dataMenu,tr("Apply named list view…"),{},[this]{namedListDialog(1);})->setObjectName("applyNamedListViewAction");
    add(dataMenu,tr("Delete named list view…"),{},[this]{namedListDialog(2);})->setObjectName("deleteNamedListViewAction");
    add(dataMenu,tr("Export list result values…"),{},[this]{if(m_model->projected())exportFile();else issue("Apply a list view first.");})->setObjectName("exportListValuesAction");
    add(dataMenu,tr("Copy list result values"),{},[this]{copyListValues();},true)->setObjectName("copyListValuesAction");
    add(dataMenu, tr("Clear list view"), {}, [this] { if (!m_busy) { m_listSpec = {}; refresh(); } }, true)->setObjectName("clearListAction");

    auto *otherReference=add(edit,tr("Pick reference from another sheet…"),{},[this]{pickOtherSheetReference();});otherReference->setObjectName("pickOtherSheetReferenceAction");
    m_pickReferenceAction=add(edit,tr("Pick formula reference"),{},[this]{if(m_pickingReference)finishReferencePick(false);else startReferencePick();});m_pickReferenceAction->setCheckable(true);m_pickReferenceAction->setObjectName("pickReferenceAction");
    auto *central = new QWidget(this); auto *layout = new QVBoxLayout(central);
    layout->setContentsMargins(4, 4, 4, 4); layout->setSpacing(4);
    auto *formula = new QHBoxLayout;
    m_address = new QLineEdit("A1"); m_address->setObjectName("address"); m_address->setAccessibleName(tr("Cell address or name")); m_address->setMaximumWidth(180); m_address->setToolTip(tr("Cell, range or name, for example B2:D20 or Quantities (up to 10,000 cells)"));
    m_source = new FormulaLineEdit; m_source->setObjectName("source"); m_source->setAccessibleName(tr("Cell input or formula")); m_source->setPlaceholderText(tr("Value or formula"));
    m_source->setMaxLength(65536);
    m_source->installEventFilter(this);
    formula->addWidget(m_address); formula->addWidget(new QLabel("fx")); formula->addWidget(m_source, 1);
    auto *pickButton=new QToolButton(central);pickButton->setObjectName("pickReferenceButton");pickButton->setDefaultAction(m_pickReferenceAction);pickButton->setText(tr("Pick reference"));pickButton->setFocusPolicy(Qt::NoFocus);pickButton->setToolTip(tr("Insert a clicked cell or dragged range into this formula draft. Release to insert; Escape cancels picking."));formula->addWidget(pickButton);
    layout->addLayout(formula);
    auto *otherPickButton=new QToolButton(central);otherPickButton->setDefaultAction(otherReference);otherPickButton->setText(tr("Other sheet…"));otherPickButton->setObjectName("pickOtherSheetReferenceButton");otherPickButton->setFocusPolicy(Qt::NoFocus);formula->addWidget(otherPickButton);
    connect(m_source,&QLineEdit::textEdited,this,[this]{++m_referenceGeneration;});
    connect(m_source,&QLineEdit::textEdited,this,&MainWindow::cancelFormatPainter);connect(m_address,&QLineEdit::textEdited,this,&MainWindow::cancelFormatPainter);connect(m_client,&RestClient::configured,this,&MainWindow::cancelFormatPainter);
    m_formulaCues=new QLabel(central);m_formulaCues->setObjectName("formulaSyntaxCues");m_formulaCues->setAccessibleName(tr("Formula references and parentheses"));m_formulaCues->setSizePolicy(QSizePolicy::Ignored,QSizePolicy::Preferred);m_formulaCues->hide();layout->addWidget(m_formulaCues);dynamic_cast<FormulaLineEdit *>(m_source)->syntaxChanged=[this]{updateFormulaReferences();};
    m_listSummary=new QLabel(central);m_listSummary->setObjectName("listCriteriaSummary");m_listSummary->setAccessibleName(tr("Active sort and filter criteria"));
    m_listSummary->setMinimumWidth(0);m_listSummary->setSizePolicy(QSizePolicy::Ignored,QSizePolicy::Preferred);m_listSummary->hide();layout->addWidget(m_listSummary);
    connect(m_model,&QAbstractItemModel::modelReset,this,&MainWindow::updateListSummary);
    m_grid = new KeyboardGridView; m_grid->setObjectName("grid"); m_grid->setAccessibleName(tr("Spreadsheet")); m_grid->setModel(m_model);
    auto *delegate=new CellDelegate(m_grid); delegate->functionCatalog=[this]{return m_functionCatalog;};delegate->syntaxChanged=[this]{updateFormulaReferences();};delegate->referenceColors=[this](int row,int col){return formulaReferenceColors(row,col);};delegate->canEdit=[this]{return !m_source->isModified();};
    delegate->enterPressed=[this](const QModelIndex &index,int direction){m_enterRequest=enterDestination(index,direction);};
    delegate->tabPressed=[this](const QModelIndex &index,int direction){
        if(!m_frozenTop || !(m_frozenTop->active() || m_frozenTop->columnActive()))return false;
        m_enterRequest=enterDestination(index,direction,true);return true;
    };
    connect(delegate,&QAbstractItemDelegate::closeEditor,this,[this]{m_enterRequest={};QTimer::singleShot(0,this,[this]{updateFormulaReferences();});});
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
        auto *delegate=new CellDelegate(pane);delegate->functionCatalog=[this]{return m_functionCatalog;};delegate->syntaxChanged=[this]{updateFormulaReferences();};delegate->referenceColors=[this](int row,int col){return formulaReferenceColors(row,col);};delegate->canEdit=[this]{return !m_source->isModified();};
        delegate->enterPressed=[this](const QModelIndex &index,int direction){m_enterRequest=enterDestination(index,direction);};
    delegate->tabPressed=[this](const QModelIndex &index,int direction){
        if(!m_frozenTop || !(m_frozenTop->active() || m_frozenTop->columnActive()))return false;
        m_enterRequest=enterDestination(index,direction,true);return true;
    };
        connect(delegate,&QAbstractItemDelegate::closeEditor,this,[this]{m_enterRequest={};QTimer::singleShot(0,this,[this]{updateFormulaReferences();});});pane->setItemDelegate(delegate);
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
    m_frozenTop->selectionController()->dataNavigation=[this](const QString &direction,bool extend){navigateData(direction,extend);};
    auto *jumpMenu=view->addMenu(tr("Jump to data boundary"));auto *extendMenu=view->addMenu(tr("Extend to data boundary"));
    for(const auto &direction:QStringList{"up","down","left","right"})for(bool extend:{false,true}) {
        const auto label=direction.left(1).toUpper()+direction.mid(1);
        auto *action=add(extend?extendMenu:jumpMenu,label,QKeySequence((extend?QString("Ctrl+Shift+"):QString("Ctrl+"))+label),[this,direction,extend]{navigateData(direction,extend);});
        action->setObjectName("data"+QString(extend?"Extend":"Jump")+label+"Action");action->setShortcutContext(Qt::WidgetShortcut);
        m_grid->addAction(action);for(auto *pane:m_frozenTop->views())pane->addAction(action);m_navigationActions.append(action);
    }
    connect(m_grid->selectionModel(),&QItemSelectionModel::selectionChanged,this,[this]{++m_navigationGeneration;if(!m_applyingNavigation)m_navigationExtending=false;});
    connect(m_model,&QAbstractItemModel::modelReset,this,[this]{if(m_painterPending || m_painterDragging || (!m_painterRows.isEmpty() && (m_model->workbook()!=m_painterOrigin["workbook"].toString() || m_model->state()!=m_painterOrigin["expected_state"].toString() || m_model->projected())))cancelFormatPainter();++m_navigationGeneration;m_navigationExtending=false;});
    connect(m_client,&RestClient::configured,this,[this]{++m_navigationFlight;++m_navigationGeneration;m_navigationPending=false;m_navigationExtending=false;});
    m_tabs = new QTabBar; m_tabs->setObjectName("sheets"); m_tabs->setAccessibleName(tr("Worksheets")); m_tabs->setExpanding(false);
    layout->addWidget(m_tabs);
    m_objectStack=new QStackedWidget(this);m_objectStack->addWidget(central);
    m_relational=new RelationalWorkbench(m_client,this);m_objectStack->addWidget(m_relational);setCentralWidget(m_objectStack);
    connect(m_relational,&RelationalWorkbench::submitRequested,this,[this](QJsonObject request){submit(request);if(m_pendingId.isEmpty())m_relational->admissionRejected();});
    connect(this,&MainWindow::operationAdmitted,m_relational,&RelationalWorkbench::operationAdmitted);
    connect(this,&MainWindow::operationReceipt,m_relational,&RelationalWorkbench::operationReceipt);
    connect(m_relational,&RelationalWorkbench::reconcileRequested,this,&MainWindow::reconcile);
    connect(m_relational,&RelationalWorkbench::cancelOperationRequested,this,[this]{
        if(m_pendingId.isEmpty())return;const auto operation=m_pendingId;
        m_client->request(base()+"/operations/"+QString::fromLatin1(QUrl::toPercentEncoding(operation))+"/cancel",{},[this,operation](QJsonObject,QString error){issue(error.isEmpty()?tr("Cancellation requested for %1. Wait for its final receipt.").arg(operation):error);});
    });
    connect(m_relational,&RelationalWorkbench::issue,this,&MainWindow::issue);
    connect(m_relational,&RelationalWorkbench::busyRequested,this,[this](bool busy){setBusy(busy);});
    connect(m_relational,&RelationalWorkbench::sheetsRequested,this,[this]{showRelational(false);});
    m_workspace=new PythonWorkspace(m_client,this);
    connect(m_workspace,&PythonWorkspace::submitRequested,this,&MainWindow::submit);
    connect(m_workspace,&PythonWorkspace::issue,this,&MainWindow::issue);
    connect(this,&MainWindow::operationAdmitted,m_workspace,&PythonWorkspace::operationAdmitted);
    connect(this,&MainWindow::operationReceipt,m_workspace,&PythonWorkspace::operationReceipt);
    connectNamesPanel(m_workspace->namesPanel());
    connect(this,&MainWindow::operationReceipt,this,[this](QJsonObject receipt){
        if(m_nameOperation.isEmpty() || receipt["operation_id"].toString()!=m_nameOperation)return;
        const auto outcome=receipt["outcome"].toString();
        if(outcome=="unknown"){nameMessage(tr("The outcome of the name change is unknown. It is reconciled before editing continues."));return;}
        m_nameOperation.clear();
        nameMessage(outcome=="committed"?m_nameSuccess:tr("%1 (%2): %3").arg(m_nameFailure,receipt["code"].toString(),receipt["message"].toString()));
    });
    connect(m_relational,&RelationalWorkbench::refreshRequested,this,[this]{refresh();});connect(m_relational,&RelationalWorkbench::sheetSelected,this,[this](const QString &id){showRelational(false);for(int i=0;i<m_tabs->count();++i)if(m_tabs->tabData(i).toString()==id){m_tabs->setCurrentIndex(i);break;}});
    m_status = new QLabel(tr("Starting local service…")); m_status->setAccessibleName(tr("Storage and calculation status"));
    m_status->setObjectName("storageStatus");
    statusBar()->addWidget(m_status, 1);
    m_summary=new QLabel(this);m_summary->setObjectName("selectionSummary");m_summary->setAccessibleName(tr("Selection summary"));
    m_summary->setMinimumWidth(0);m_summary->setSizePolicy(QSizePolicy::Ignored,QSizePolicy::Preferred);
    statusBar()->addPermanentWidget(m_summary,1);
    m_summaryTimer=new QTimer(this);m_summaryTimer->setSingleShot(true);m_summaryTimer->setInterval(250);
    connect(m_summaryTimer,&QTimer::timeout,this,&MainWindow::requestSummary);
    connect(m_grid->selectionModel(),&QItemSelectionModel::selectionChanged,this,&MainWindow::queueSummary);
    connect(m_grid->selectionModel(),&QItemSelectionModel::selectionChanged,this,&MainWindow::updateAddress);
    connect(m_model,&QAbstractItemModel::modelReset,this,&MainWindow::queueSummary);
    connect(m_client,&RestClient::configured,this,[this]{
        ++m_summaryFlight;m_summaryInFlight=false;m_summaryKey={};queueSummary();
    });
    m_hiddenButton=new QPushButton(this); m_hiddenButton->setObjectName("showHiddenButton");
    m_hiddenButton->hide(); statusBar()->addPermanentWidget(m_hiddenButton);
    connect(m_hiddenButton,&QPushButton::clicked,this,[this]{showHidden(true,true);});
    auto *issuesDock = new QDockWidget(tr("Issues"), this); issuesDock->setObjectName("issuesDock");
    m_issues = new QPlainTextEdit; m_issues->setObjectName("operationIssues");m_issues->setReadOnly(true); m_issues->setAccessibleName(tr("Operation and calculation issues")); issuesDock->setWidget(m_issues);
    addDockWidget(Qt::BottomDockWidgetArea, issuesDock); issuesDock->hide(); view->addAction(issuesDock->toggleViewAction());
    view->addAction(m_toolbar->toggleViewAction());
    add(help, tr("Preview capabilities"), {}, [this] {
        m_client->request("/v1/capabilities", {}, [this](QJsonObject, QString error) {
            if (!error.isEmpty()) issue(error);
            else QMessageBox::information(this, tr("Development preview"), tr("Qt Widgets/C++ desktop • SQLite • Python REST service\n\nAvailable: cell entry, bounded viewports, the published formula tier, copy/paste, fill, wrap/borders, cell Undo/Redo, bounded sheet and row/column edits, list views, scoped search, CSV and bounded XLSX/ODS selection/workbook interchange. Sheet sizes, hiding and frozen panes use local desktop preferences.\n\nUnavailable: unrestricted reference transforms, managed SQL, workbook code authoring and remote binding.\n\nThese limits are recorded in docs/design/implementation-status.md."));
        }, false);
    });
    add(help, tr("About DataHorizon"), {}, [this] { QMessageBox::about(this, tr("DataHorizon"), tr("DataHorizon 0.1 development preview\nAll DataHorizon components: GNU GPL version 3 only.\nThird-party dependencies retain their licenses.\nSee repository licensing materials.")); });

    add(view,tr("Read current cell…"),QKeySequence(tr("Ctrl+Shift+I")),[this]{
        const auto index=m_grid->currentIndex();
        const auto text=index.isValid()?m_model->data(index,Qt::AccessibleTextRole).toString():tr("No current cell");
        QMessageBox dialog(QMessageBox::Information,tr("Current cell"),text,QMessageBox::Ok,this);
        dialog.setTextFormat(Qt::PlainText);dialog.setObjectName("currentCellDescription");dialog.exec();
    })->setObjectName("readCurrentCellAction");
    connect(m_grid->selectionModel(), &QItemSelectionModel::currentChanged, this, [this](const QModelIndex &, const QModelIndex &previous) {
        ++m_navigationGeneration;if(!m_applyingNavigation)m_navigationExtending=false;
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
        // A cell address always wins; anything else is looked up as a name.
        const auto text=m_address->text().trimmed();
        const auto match=QRegularExpression("^\\$?([A-Za-z]{1,3})\\$?([1-9][0-9]{0,6})(?::\\$?([A-Za-z]{1,3})\\$?([1-9][0-9]{0,6}))?$").match(text);
        if(!match.hasMatch()) {
            if(!m_namesReady){issue(tr("Names are being read. Try again in a moment."));return;}
            const auto name=nameByText(text);
            if(!name.isEmpty()){goToName(name);return;}
            static const QRegularExpression identifier(QStringLiteral("^[A-Za-z_][A-Za-z0-9_]*$"));
            if(identifier.match(text).hasMatch()){issue(tr("No cell address or name matches %1. Use a cell, a range such as B2:D20, or a name from the Name manager.").arg(text));return;}
            issue("Use a cell or rectangular range within A1:XFD1048576, such as B2:D20.");return;
        }
        auto column=[](const QString &name){int value=0;for(auto ch:name.toUpper())value=value*26+ch.unicode()-'A'+1;return value;};
        const int row=match.captured(2).toInt(),col=column(match.captured(1));
        const int endRow=match.captured(4).isEmpty()?row:match.captured(4).toInt();
        const int endCol=match.captured(3).isEmpty()?col:column(match.captured(3));
        if(qMax(row,endRow)>1048576 || qMax(col,endCol)>16384) {
            issue("Use a cell or rectangular range within A1:XFD1048576, such as B2:D20.");return;
        }
        goToRange(qMin(row,endRow),qMin(col,endCol),qMax(row,endRow),qMax(col,endCol),row,col);
    });
    auto referenceViews=m_frozenTop->views();referenceViews.append(m_grid);for(auto *pane:referenceViews){pane->installEventFilter(this);pane->viewport()->installEventFilter(this);}
    connect(m_model,&QAbstractItemModel::modelAboutToBeReset,this,[this]{finishReferencePick(false);});
    connect(m_client,&RestClient::configured,this,[this]{finishReferencePick(false);});
    setBusy(true);
}

void MainWindow::fitVisibleWrappedRows() {
    updateVisibleCalculations();
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

void MainWindow::updateVisibleCalculations() {
    if(!m_frozenTop)return;
    auto views=m_frozenTop->views();views.append(m_grid);QList<QRect> ranges;
    for(auto *view:views) {
        if(!view->isVisible() || m_model->rowCount()==0)continue;
        const int firstRow=qMax(0,view->rowAt(0)),firstCol=qMax(0,view->columnAt(0));
        int lastRow=view->rowAt(view->viewport()->height()-1),lastCol=view->columnAt(view->viewport()->width()-1);
        if(lastRow<0)lastRow=qMin(m_model->rowCount()-1,firstRow+512);
        if(lastCol<0)lastCol=qMin(m_model->columnCount()-1,firstCol+256);
        ranges.append(QRect(firstCol,firstRow,lastCol-firstCol+1,lastRow-firstRow+1));
    }
    m_model->setVisibleRanges(ranges);
}

QString MainWindow::viewSettingsPath() const {
    if(!m_options.viewStatePath.isEmpty()) return m_options.viewStatePath;
    return qEnvironmentVariable("DATAHORIZON_VIEW_STATE",QStandardPaths::writableLocation(QStandardPaths::AppConfigLocation)+"/views.ini");
}

void MainWindow::portableView(bool save) {
    if(m_busy || m_unknown || m_info.isEmpty() || (save && m_info["readonly"].toBool()))return;
    const auto target=targetContext();const auto key=m_viewKey;captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target))return;
    if(save){saveViewState();QSettings settings(viewSettingsPath(),QSettings::IniFormat);const auto payload=QJsonDocument::fromJson(settings.value(key).toByteArray()).object();auto request=target;request["action"]="metadata_set";request["namespace"]="presentation";request["id"]="view";request["payload"]=payload;request["label"]="Save portable view";submit(request);return;}
    auto request=target;request.remove("workbook");request["expected_state"]=target["expected_state"];
    m_client->request(base()+"/metadata/read",request,[this,target,key](QJsonObject result,QString error){
        if(!error.isEmpty()){issue(error);return;}if(!matchesTarget(target) || m_viewKey!=key || m_source->isModified())return;
        QJsonObject view;bool found=false;for(const auto &value:result["objects"].toArray()){const auto item=value.toObject();if(item["namespace"]=="presentation" && item["id"]=="view"){view=item["payload"].toObject();found=true;}}
        if(!found){issue("No portable view is stored for this sheet.");return;}
        QScopedValueRollback<bool> applying(m_applyingView,true);
        for(int row:m_sizedRows)m_grid->setRowHeight(row,26);for(int col:m_sizedColumns)m_grid->setColumnWidth(col,100);
        for(int row:m_hiddenRows)m_grid->setRowHidden(row,false);for(int col:m_hiddenColumns)m_grid->setColumnHidden(col,false);
        m_sizedRows.clear();m_sizedColumns.clear();QSettings settings(viewSettingsPath(),QSettings::IniFormat);settings.setValue(key,QJsonDocument(view).toJson(QJsonDocument::Compact));settings.sync();m_viewKey.clear();restoreViewState();m_status->setText(tr("Loaded portable view and named lists • Inputs retained"));
    });
}

void MainWindow::saveViewState() {
    if(m_viewKey.isEmpty()) return;
    auto encode=[](const QMap<int,int> &sizes){
        QJsonObject result; for(auto it=sizes.begin();it!=sizes.end();++it) result[QString::number(it.key())]=it.value(); return result;
    };
    QSettings settings(viewSettingsPath(),QSettings::IniFormat);
    auto hidden=[](const QSet<int> &values){QJsonArray result; QList<int> sorted=values.values(); std::sort(sorted.begin(),sorted.end()); for(int value:sorted) result.append(value); return result;};
    settings.setValue(m_viewKey,QJsonDocument(QJsonObject{{"rows",encode(m_rowSizes)},{"columns",encode(m_columnSizes)},
        {"hidden_rows",hidden(m_hiddenRows)},{"hidden_columns",hidden(m_hiddenColumns)},{"freeze_top_row",m_frozenRows>0},{"freeze_first_column",m_frozenColumns>0},{"frozen_rows",m_frozenRows},{"frozen_columns",m_frozenColumns},{"named_views",m_namedViews}}).toJson(QJsonDocument::Compact));
    settings.sync();
    if(settings.status()!=QSettings::NoError && !m_shuttingDown) issue("Could not save desktop view preferences. Workbook data remains stored.");
}

void MainWindow::restoreViewState() {
    QScopedValueRollback<bool> applying(m_applyingView,true);
    const auto identity=m_info["path"].toString()+"/"+m_info["workbook_id"].toString()+"/"+m_model->sheet();
    const auto key="views/v1/"+QString::fromLatin1(QCryptographicHash::hash(identity.toUtf8(),QCryptographicHash::Sha256).toHex());
    if(key!=m_viewKey) {
        saveViewState(); m_viewKey=key; m_rowSizes.clear(); m_columnSizes.clear();m_namedViews={};
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
        const auto views=state.value("named_views");bool invalid=false;
        if(!views.isUndefined()) {
            if(!views.isObject() || views.toObject().size()>16 || QJsonDocument(views.toObject()).toJson(QJsonDocument::Compact).size()>128*1024)invalid=true;
            else {const auto names=views.toObject();for(auto it=names.begin();it!=names.end();++it){QJsonObject scope;if(validListViewName(it.key()) && it.value().isObject() && normalizedListView(it.value().toObject(),scope))m_namedViews[it.key()]=scope;else invalid=true;}}
        }
        if(invalid)issue("Invalid saved list view preferences were ignored. Workbook data is unchanged.");
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

void MainWindow::navigateData(const QString &direction,bool extend) {
    if(m_busy || m_unknown || m_info.isEmpty() || m_navigationPending)return;
    if(m_model->projected()){issue("Clear the list view before source data-boundary navigation.");return;}
    const auto start=m_grid->currentIndex();if(!start.isValid())return;
    const auto target=targetContext();captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || m_unknown || !matchesTarget(target) || m_grid->currentIndex()!=start)return;
    const auto generation=m_navigationGeneration,flight=++m_navigationFlight;
    if(!extend || !m_navigationExtending)m_navigationAnchor=start;
    const auto anchor=m_navigationAnchor;m_navigationPending=true;
    auto request=target;request.remove("workbook");request["row"]=start.row()+1;request["col"]=start.column()+1;request["direction"]=direction;
    m_client->request(base()+"/navigate",request,[this,target,start,anchor,generation,flight,extend,direction](QJsonObject result,QString error){
        if(flight!=m_navigationFlight)return;m_navigationPending=false;
        if(generation!=m_navigationGeneration || m_busy || m_unknown || !matchesTarget(target) || m_grid->currentIndex()!=start || m_source->isModified())return;
        QList<QTableView *> views=m_frozenTop->views();views.append(m_grid);
        for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))if(delegate->hasEditor())return;
        if(!error.isEmpty()){issue(error);if(result["code"]=="STALE_STATE")m_model->externalChange();return;}
        const int row=result["row"].toInt(),col=result["col"].toInt();
        const bool vertical=direction=="up" || direction=="down";
        if(result["state"]!=target["expected_state"] || result["sheet"]!=target["sheet"] || result["basis"]!="stored_inputs"
            || !result["row"].isDouble() || result["row"].toDouble()!=row || !result["col"].isDouble() || result["col"].toDouble()!=col
            || row<1 || row>1048576 || col<1 || col>16384 || (vertical?col!=start.column()+1:row!=start.row()+1)) {issue("Invalid or stale navigation result. Refresh and try again.");return;}
        if(intersectsHidden(row,col,row,col)){issue("The data boundary is hidden. Show its row/column before navigating there.");return;}
        QScopedValueRollback<bool> applying(m_applyingNavigation,true);
        if(m_frozenTop->selectionController()->navigateTo(m_model->index(row-1,col-1),extend,extend?QModelIndex(anchor):QModelIndex()))m_navigationExtending=extend;
    });
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

QList<QColor> MainWindow::formulaReferenceColors(int row,int column) const {
    QList<QColor> result;if(!m_tabs || m_info.isEmpty())return result;const int sourceRow=m_model->sourceRow(row)+1;const auto name=m_tabs->tabText(m_tabs->currentIndex());
    for(const auto &cue:m_formulaReferences)if((cue.sheet.isEmpty() || cue.sheet==m_model->sheet() || cue.sheet.toCaseFolded()==name.toCaseFolded()) && sourceRow>=cue.r1 && sourceRow<=cue.r2 && column+1>=cue.c1 && column+1<=cue.c2 && !result.contains(cue.color)){result.append(cue.color);if(result.size()==3)break;}
    return result;
}

void MainWindow::updateFormulaReferences() {
    if(m_shuttingDown || !m_formulaCues || !m_frozenTop)return;auto *line=dynamic_cast<FormulaLineEdit *>(m_source);auto views=m_frozenTop->views();views.append(m_grid);
    for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))if(delegate->activeEditor())line=delegate->activeEditor();
    const auto syntax=line?line->syntaxCues():FormulaSyntaxCues{};m_formulaReferences=syntax.references;QStringList references;
    for(int i=0;i<qMin(8,syntax.references.size());++i)references.append(syntax.references[i].source);QString text;if(!references.isEmpty())text=tr("References: %1").arg(references.join(" • "));if(syntax.references.size()>8)text+=tr(" • +%1 more").arg(syntax.references.size()-8);
    if(!syntax.parentheses.isEmpty())text+=(text.isEmpty()?QString():QString(" • "))+syntax.parentheses;if(syntax.truncated)text+=tr(" • Visual cue limit reached (64 references / nesting 128)");m_formulaCues->setText(text.left(300));m_formulaCues->setToolTip(text);m_formulaCues->setVisible(!text.isEmpty());m_grid->setProperty("formulaReferenceCount",syntax.references.size());for(auto *view:views)view->viewport()->update();
}

void MainWindow::startReferencePick() {
    cancelFormatPainter();
    ++m_referenceGeneration;
    QSignalBlocker blocker(m_pickReferenceAction);m_pickReferenceAction->setChecked(false);
    if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool() || m_model->projected()){issue("Reference picking needs an editable source sheet.");return;}
    auto *line=dynamic_cast<FormulaLineEdit *>(m_source);auto views=m_frozenTop->views();views.append(m_grid);
    for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))if(delegate->activeEditor())line=delegate->activeEditor();
    if(!line || !line->isEnabled() || !line->formulaAllowed() || !line->text().startsWith('=')){issue("Edit a formula first, then place the cursor at an operand or select text to replace.");return;}
    int start=line->selectionStart();int length=start<0?0:line->selectedText().size();if(start<0)start=line->cursorPosition();if(start==0 && length>0){start=1;--length;}const auto context=formulaCursorContext(line->text(),start);
    if(context.quoted || (length==0 && context.start<0)){issue("Place the formula cursor outside quoted text at an operand, or select the reference to replace.");return;}
    if(length==0){start=context.start;length=context.end-context.start;}
    line->dismissAssistance();captureGridDraft(false);syncSource();
    if(!m_source->isEnabled() || m_draftTarget["workbook"]!=m_info["id"] || m_draftTarget["state"]!=m_info["state"] || m_draftTarget["input_kind"].toString()=="text")return;
    m_referenceTarget=targetContext();m_referenceCurrent=m_grid->currentIndex();m_referenceText=m_source->text();m_referenceStart=start;m_referenceLength=length;m_referencePreview.clear();m_referenceIssue.clear();m_referenceDragging=false;m_pickingReference=true;m_pickReferenceAction->setChecked(true);
    m_source->setFocus();m_source->setSelection(start,length);m_status->setText("Pick a cell or drag a range • Release inserts into the original formula draft • Escape cancels picking");
}

void MainWindow::pickOtherSheetReference() {
    if(m_info["sheets"].toArray().size()<2 || m_info["sheets"].toArray().size()>64){issue("Reference picking needs 2–64 sheets in this tier.");return;}
    startReferencePick();if(!m_pickingReference)return;
    const auto scope=m_referenceTarget;const auto current=m_referenceCurrent;const auto source=m_referenceText;const int start=m_referenceStart,length=m_referenceLength;const bool modified=m_source->isModified();const auto service=m_client->url();const auto generation=m_referenceGeneration;finishReferencePick(false);
    ReferencePickerDialog dialog(m_client,m_info["id"].toString(),m_info["state"].toString(),m_info["sheets"].toArray(),m_model->sheet(),this);
    if(dialog.exec()!=QDialog::Accepted){m_source->setFocus();return;}
    const auto range=dialog.range();const auto sheet=dialog.sheet(),reference=dialog.reference();if(range.isEmpty() || reference.isEmpty())return;
    m_status->setText("Checking picked reference…");
    m_client->request(base()+"/range",{{"sheet",sheet},{"r1",range["r1"]},{"r2",range["r1"]},{"c1",range["c1"]},{"c2",range["c1"]},{"preview",true}},[this,scope,current,source,start,length,modified,service,generation,sheet,reference](QJsonObject result,QString error){
        if(generation!=m_referenceGeneration || service!=m_client->url())return;
        if(m_busy || m_unknown || !matchesTarget(scope) || current!=m_grid->currentIndex() || m_model->projected() || !m_source->isEnabled() || m_source->text()!=source || m_source->isModified()!=modified || m_source->cursorPosition()!=start+length || m_source->selectionStart()!=(length?start:-1))return;
        if(!error.isEmpty() || result["state"]!=scope["expected_state"] || result["sheet"].toString()!=sheet){issue(error.isEmpty()?"Workbook changed. Refresh and pick the reference again.":error);return;}
        const auto replacement=source.left(start)+reference+source.mid(start+length);if(replacement.size()>m_source->maxLength() || replacement.toUtf8().size()>65536){issue("Picked reference exceeds the formula input limit. Nothing inserted.");return;}
        m_source->setFocus();m_source->setCursorPosition(0);m_source->setSelection(start,length);m_source->insert(reference);m_source->setModified(true);m_status->setText("Cross-sheet reference inserted into original draft • Enter to submit, Escape to discard");
    });
}

void MainWindow::finishReferencePick(bool insert) {
    if(!m_pickingReference)return;m_pickingReference=false;m_referenceDragging=false;QSignalBlocker blocker(m_pickReferenceAction);m_pickReferenceAction->setChecked(false);
    const bool valid=!m_busy && !m_unknown && !m_model->projected() && matchesTarget(m_referenceTarget) && m_referenceCurrent==m_grid->currentIndex() && m_source->text()==m_referenceText && m_source->cursorPosition()==m_referenceStart+m_referenceLength && m_source->selectionStart()==(m_referenceLength?m_referenceStart:-1) && m_source->isEnabled();
    if(insert && valid && m_referenceIssue.isEmpty() && !m_referencePreview.isEmpty()){
        m_source->setFocus();m_source->setCursorPosition(0);m_source->setSelection(m_referenceStart,m_referenceLength);m_source->insert(m_referencePreview);m_source->setModified(true);if(auto *line=dynamic_cast<FormulaLineEdit *>(m_source))line->updateAssistance(false);m_status->setText("Reference inserted into formula draft • Enter to submit, Escape to discard");
    }else if(insert && !m_referenceIssue.isEmpty())issue(m_referenceIssue);
    else if(insert && !valid)issue("Reference picking inputs changed. No reference inserted; review the draft.");
    else m_status->setText("Reference picking cancelled • Draft retained");
    m_referenceTarget={};m_referencePreview.clear();m_referenceIssue.clear();
}

bool MainWindow::referenceMouseEvent(QObject *object,QEvent *event) {
    if(event->type()!=QEvent::MouseButtonPress && event->type()!=QEvent::MouseButtonRelease && event->type()!=QEvent::MouseMove)return false;
    auto views=m_frozenTop->views();views.append(m_grid);bool viewport=false;for(auto *view:views)if(object==view->viewport())viewport=true;if(!viewport)return false;
    const auto *mouse=static_cast<QMouseEvent *>(event);if(event->type()==QEvent::MouseButtonPress && mouse->button()!=Qt::LeftButton)return false;
    if(event->type()==QEvent::MouseMove && !m_referenceDragging)return false;
    if(event->type()==QEvent::MouseButtonRelease && !m_referenceDragging)return false;
    QModelIndex cell;for(auto *view:views){if(!view->isVisible())continue;const auto point=view->viewport()->mapFromGlobal(mouse->globalPosition().toPoint());if(view->viewport()->rect().contains(point)){cell=view->indexAt(point);break;}}
    if(event->type()==QEvent::MouseButtonPress){if(!cell.isValid())return true;m_referenceAnchor=QPoint(cell.column()+1,cell.row()+1);m_referenceDragging=true;}
    if(cell.isValid() && m_referenceDragging){const int r1=qMin(m_referenceAnchor.y(),cell.row()+1),r2=qMax(m_referenceAnchor.y(),cell.row()+1),c1=qMin(m_referenceAnchor.x(),cell.column()+1),c2=qMax(m_referenceAnchor.x(),cell.column()+1);
        m_referenceIssue=qint64(r2-r1+1)*(c2-c1+1)>10000?tr("Picked reference exceeds 10,000 cells. Nothing inserted."):intersectsHidden(r1,c1,r2,c2)?tr("Show hidden cells before picking this range. Nothing inserted."):QString();
        m_referencePreview=SheetModel::columnName(c1)+QString::number(r1);if(r1!=r2 || c1!=c2)m_referencePreview+=":"+SheetModel::columnName(c2)+QString::number(r2);
        m_status->setText(m_referenceIssue.isEmpty()?tr("Reference preview %1 • Release to insert").arg(m_referencePreview):m_referenceIssue);
    }
    if(event->type()==QEvent::MouseButtonRelease){if(!cell.isValid())m_referenceIssue="Release over a visible cell. Nothing inserted.";finishReferencePick(true);}return true;
}

void MainWindow::loadFunctionCatalog() {
    m_client->request("/v1/functions",{},[this](QJsonObject catalog,QString error){
        if(!error.isEmpty() || !validFunctionCatalog(catalog))return;
        m_functionCatalog=catalog;for(auto *line:findChildren<QLineEdit *>())if(auto *formula=dynamic_cast<FormulaLineEdit *>(line))formula->setCatalog(catalog);
    },false);
}

void MainWindow::showFunctionHelp() {
    if(m_client->url().isEmpty()){issue("Connect to a service to read its implemented function catalog.");return;}
    m_client->request("/v1/functions",{},[this](QJsonObject catalog,QString error){
        if(!error.isEmpty() || !validFunctionCatalog(catalog)){issue(error.isEmpty()?"Invalid function catalog response.":error);return;}
        m_functionCatalog=catalog;for(auto *line:findChildren<QLineEdit *>())if(auto *formula=dynamic_cast<FormulaLineEdit *>(line))formula->setCatalog(catalog);FunctionHelpDialog dialog(catalog,this);dialog.exec();
    },false);
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

void MainWindow::openBook(const QString &path, bool create, bool readonly,bool repair) {
    if(repair && m_refreshFailed && !m_unknown && m_pendingId.isEmpty())setBusy(false);
    if (m_busy || m_unknown) return;
    // Python drafts are decided first, but a Discard only takes effect once the
    // other workbook has actually opened; every earlier return keeps them.
    const bool otherWorkbook=!m_info.isEmpty() && QFileInfo(path).canonicalFilePath()!=QFileInfo(m_info["path"].toString()).canonicalFilePath();
    if(otherWorkbook && !m_workspace->resolveDrafts())return;
    auto keepDrafts=qScopeGuard([this]{m_workspace->keepDrafts();});
    if(!discardQueryDraft())return;
    if(m_queryClosing)return;
    if(m_relational->hasQueryJob()){
        keepDrafts.dismiss();
        m_queryClosing=true;m_relational->closeQuery([this,path,create,readonly,repair](QString error){m_queryClosing=false;if(!error.isEmpty()){m_workspace->keepDrafts();issue(error);return;}openBook(path,create,readonly,repair);});return;
    }
    if(m_relational->hasActiveEditor())return;
    if(!m_relational->retainedDraft().isEmpty()){
        if(QMessageBox::question(this,tr("Discard table draft?"),tr("Discard the retained table draft before opening another workbook?"))!=QMessageBox::Yes)return;
        m_relational->restoreDraft({});
    }
    captureGridDraft(false);
    if (m_source->isModified() || !m_recoveredDraft.isEmpty()) {
        if (QMessageBox::question(this,tr("Discard editor draft?"),tr("Discard the unsubmitted formula-bar draft before opening another workbook? Stored edits remain in the current workbook."))!=QMessageBox::Yes) return;
        m_source->setModified(false); clearRecoveredDraft();
    }
    m_enterRequest={}; m_enterNavigation={};
    if(!m_info.isEmpty() && QFileInfo(path).canonicalFilePath()==QFileInfo(m_info["path"].toString()).canonicalFilePath() && (m_info["repair"].toBool()!=repair || m_info["readonly"].toBool()!=readonly)){
        setBusy(true,tr("Releasing the previous workbook mode…"));m_client->request(base()+"/close",{},[this,path,create,readonly,repair](QJsonObject,QString error){if(!error.isEmpty()){setBusy(false);issue(error);return;}m_info={};clearNames();m_model->bind({},{},{},true);m_relational->bind({});m_workspace->bind({});setBusy(false);openBook(path,create,readonly,repair);});return;
    }
    keepDrafts.dismiss();
    setBusy(true, "Opening workbook…");
    m_client->request("/v1/open", {{"path", path}, {"create", create}, {"readonly", readonly},{"repair",repair}}, [this](QJsonObject info, QString error) {
        if (!error.isEmpty()) { m_workspace->keepDrafts(); setBusy(false, "Open failed"); issue(error); return; }
        m_workspace->commitDiscard();
        if (!m_info.isEmpty()) m_client->request(base()+"/close",{},[](QJsonObject,QString){});
        m_listSpec = {}; acceptInfo(info);
    });
}

void MainWindow::acceptInfo(const QJsonObject &info, const QString &selectSheet) {
    m_refreshFailed=false;
    QScopedValueRollback<bool> applying(m_applyingView,true);
    const auto oldSheet = m_restoreDraft ? m_recoveredDraft["sheet"].toString() : selectSheet.isEmpty() ? m_model->sheet() : selectSheet;
    if (m_restoreDraft) m_listSpec={};
    const auto previous = m_grid->currentIndex();
    const int previousRow=previous.isValid()?m_model->sourceRow(previous.row()):0;
    const int previousColumn=previous.isValid()?previous.column():0;
    if (info["id"]!=m_info["id"]) m_released=false;
    if(m_info["id"]!=info["id"] && m_relationalActive)showRelational(false);
    m_info = info;
    m_relational->bind(info);
    m_workspace->bind(info);
    readNames(info);
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
    setBusy(false, info["readonly"].toBool() ? "Read-only • Visible formulas calculate in background" : "Stored automatically • Visible formulas calculate in background");
    m_undo->setEnabled(info["undo"].isString() && !info["readonly"].toBool());
    m_redo->setEnabled(info["redo"].isString() && !info["readonly"].toBool());
    setWindowTitle(QFileInfo(info["path"].toString()).fileName() + " — DataHorizon development preview");
    syncSource();
    if(info["repair"].toBool() && !m_relationalActive)showRelational(true);
    if (m_restoreDraft) restoreRecoveredDraft();
    if(!m_relationalAfterWrite.isEmpty()) {
        const bool open=m_relationalAfterWrite==info["id"].toString();m_relationalAfterWrite.clear();
        if(open && !m_relationalActive)showRelational(true);
    }
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
        if (!error.isEmpty()) { m_refreshFailed=true;setBusy(true, "Refresh failed • Editing stopped"); issue(error); return; }
        acceptInfo(info, selectSheet);
    }, false);
}

void MainWindow::submit(QJsonObject operation) {
    if (m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool()) return;
    const auto action=operation["action"].toString();
    if(m_relationalActive && !action.startsWith("module_") && !action.startsWith("name_") && !(action.startsWith("table_") || action.startsWith("index_") || action.startsWith("relationship_") || action.startsWith("query_") || action=="undo" || action=="redo")) {
        issue(tr("Return to sheets before applying a sheet operation."));return;
    }
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
    emit operationAdmitted(m_pendingId);
    operation["operation_id"] = m_pendingId; operation["expected_state"] = m_info["state"];
    operation["sheet"] = m_model->sheet();
    setBusy(true, "Submitting • " + m_pendingId);
    const auto operationId=m_pendingId;
    m_client->request(base()+"/operations", operation, [this,operationId,action](QJsonObject result, QString error) {
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
            emit operationReceipt(m_unknown?QJsonObject{{"operation_id",operationId},{"outcome","unknown"},{"message",error}}:result);
            emit operationFinished(false); return;
        }
        if (m_enterNavigation["operation_id"].toString()==operationId) m_enterNavigation["ready"]=true;
        if (m_draftSubmissionId==operationId) clearRecoveredDraft();
        clearRecoveryRecord(); m_pendingId.clear(); m_unknown = false; m_info["state"] = result["state"];
        if(action=="table_promote" || action=="table_create") {
            m_relational->selectTable(result["table"].toObject()["id"].toString());m_relationalAfterWrite=m_info["id"].toString();
            if(action=="table_promote")m_tablePromotionDraft={};
        }
        reportReferenceWarnings(result);
        refresh(); emit operationReceipt(result);emit operationFinished(true);
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
        emit operationReceipt(result);
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
    const auto data=QJsonDocument(QJsonObject{{"workbook_path",m_info["path"]},{"operation_id",m_pendingId},{"outcome","unknown"},{"editor_draft",m_recoveredDraft},{"table_draft",m_relational->retainedDraft()},{"query_draft",m_relational->queryDraft()},
        {"instructions","Reopen the workbook and look up this operation ID before retrying any mutation."}}).toJson();
    if (!file.open(QIODevice::WriteOnly)) { issue(file.errorString()); return false; }
    file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
    if (file.write(data)!=data.size() || !file.commit()) { issue(file.errorString()); return false; }
    return true;
}

bool MainWindow::preserveJobRecovery(const QJsonObject &workbook,const QString &job,const QJsonObject &status) {
    const auto dir=recoveryDirectory();
    if(!QDir().mkpath(dir)){issue("Cannot create job recovery directory: "+dir);return false;}
    if(m_jobRecoveryRecord.isEmpty())m_jobRecoveryRecord=dir+"/job-"+QUuid::createUuid().toString(QUuid::WithoutBraces)+".json";
    QSaveFile file(m_jobRecoveryRecord);
    const auto data=QJsonDocument(QJsonObject{{"workbook_path",workbook["path"]},{"workbook_id",workbook["workbook_id"]},{"handle",workbook["id"]},{"job_id",job},{"status",status},{"outcome","unknown"},{"instructions","Inspect this native file's retained job journal and native receipts before resuming. Closing did not confirm cancellation or handle release."}}).toJson();
    if(!file.open(QIODevice::WriteOnly)){issue(file.errorString());return false;}
    file.setPermissions(QFileDevice::ReadOwner|QFileDevice::WriteOwner);
    if(file.write(data)!=data.size() || !file.commit()){issue(file.errorString());return false;}
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
    busy=busy || !m_activeOperationJob.isEmpty();
    if(busy){finishReferencePick(false);cancelFormatPainter();}
    m_busy = busy || m_releasing;
    if(m_busy){clearFindResults();++m_navigationGeneration;m_navigationExtending=false;}
    for(auto *action:m_navigationActions)action->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected());
    m_model->setBusy(m_busy); m_toolbar->setEnabled(!m_busy && !m_relationalActive); m_tabs->setEnabled(!m_busy);
    if(m_relational)m_relational->setLocked(m_busy || m_unknown);
    if(m_workspace)m_workspace->setLocked(m_busy || m_unknown);
    if(m_workspace)updateNameControls();
    if(m_freezePanesAction)m_freezePanesAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected());
    if(m_unfreezePanesAction)m_unfreezePanesAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && (m_frozenRows>0 || m_frozenColumns>0));
    if(m_freezeColumnAction)m_freezeColumnAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected() && !m_hiddenColumns.contains(0));
    if(m_freezeAction) m_freezeAction->setEnabled(!m_busy && !m_unknown && !m_info.isEmpty() && !m_model->projected() && !m_hiddenRows.contains(0));
    syncSource();
    if (!message.isEmpty()) m_status->setText(m_releasing ? "Closing workbook…" : message);
    queueSummary();updateListSummary();
}

void MainWindow::issue(const QString &message) {
    m_issues->appendPlainText(message);
    if (auto *dock = qobject_cast<QDockWidget *>(m_issues->parentWidget())) dock->show();
}

void MainWindow::syncSource() {
    ensureVisibleCurrent();
    const bool addressDraft=m_address->hasFocus() && m_address->isModified();
    auto index = m_grid->currentIndex();
    m_grid->setAccessibleDescription(index.isValid()?m_model->data(index,Qt::AccessibleTextRole).toString():tr("No current cell"));
    if (!index.isValid()) {
        m_source->clear(); m_source->setModified(false); m_source->setEnabled(false); m_draftTarget={};
        if(!addressDraft){m_address->clear();m_address->setModified(false);}
        return;
    }
    if(auto *line=dynamic_cast<FormulaLineEdit *>(m_source))line->setFormulaAllowed(m_model->cell(index)["input_kind"].toString()!="text");
    m_model->ensureSource(index);
    const bool ready=m_model->sourceReady(index);
    m_source->setEnabled(!m_busy && !m_info["readonly"].toBool() && ready);
    if(!addressDraft) m_address->setText(addressText(index));
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

QJsonObject MainWindow::enterDestination(const QModelIndex &index, int direction, bool horizontal) const {
    const auto next=m_model->index(index.row()+(horizontal?0:direction),index.column()+(horizontal?direction:0));
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
        cell["source"]=source; cell["kind"]=sameSource || QStringList{"text","date","time","datetime"}.contains(inputKind)?inputKind:QString("auto");
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
    if(m_pickingReference)finishReferencePick(false);
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
    QStringList broken;for(const auto &value:receipt["broken_names"].toArray())broken.append(value.toString());
    if(!broken.isEmpty())issue(tr("Names that no longer refer to any cells: %1").arg(broken.join(", ")));
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

void MainWindow::cancelFormatPainter() {
    ++m_painterGeneration;m_painterPending=false;m_painterDragging=false;m_painterRows={};m_painterOrigin={};m_painterDestination={};
    if(m_painterAction){m_painterAction->setChecked(false);m_painterAction->setProperty("ready",false);}if(m_painterCancel)m_painterCancel->setEnabled(false);
    if(m_frozenTop){auto views=m_frozenTop->views();views.append(m_grid);for(auto *view:views)view->viewport()->unsetCursor();}
}

void MainWindow::startFormatPainter() {
    if(m_painterPending || !m_painterRows.isEmpty()){cancelFormatPainter();return;}
    cancelFormatPainter();if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool())return;
    if(m_address->isModified()){issue("Finish address entry before copying formatting.");return;}
    const auto range=selectedRange();if(range.isEmpty() || m_grid->selectionModel()->selection().size()!=1)return;const auto target=targetContext(range);const auto selection=m_grid->selectionModel()->selection();const auto current=QPersistentModelIndex(m_grid->currentIndex());const auto service=m_client->url();
    captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target) || current!=m_grid->currentIndex() || selection!=m_grid->selectionModel()->selection())return;
    const auto generation=++m_painterGeneration,navigation=m_navigationGeneration,reference=m_referenceGeneration;m_painterPending=true;m_painterAction->setChecked(true);m_painterCancel->setEnabled(true);auto request=target;request.remove("workbook");m_status->setText(tr("Reading selected formatting…"));
    m_client->request(base()+"/styles",request,[this,target,selection,current,service,generation,navigation,reference](QJsonObject result,QString error){
        if(generation!=m_painterGeneration)return;
        if(m_busy || m_unknown || !matchesTarget(target) || service!=m_client->url() || navigation!=m_navigationGeneration || reference!=m_referenceGeneration || current!=m_grid->currentIndex() || selection!=m_grid->selectionModel()->selection() || m_source->isModified() || m_address->isModified() || m_model->projected()){cancelFormatPainter();return;}
        auto views=m_frozenTop->views();views.append(m_grid);for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))if(delegate->hasEditor()){cancelFormatPainter();return;}
        const auto rows=result["rows"].toArray();const QJsonArray rectangle{target["r1"],target["c1"],target["r2"],target["c2"]};
        if(!error.isEmpty() || result["state"]!=target["expected_state"] || result["sheet"]!=target["sheet"] || (!result["range"].isArray() || result["range"].toArray()!=rectangle) || !validStylePattern(rows,target["r2"].toInt()-target["r1"].toInt()+1,target["c2"].toInt()-target["c1"].toInt()+1)){cancelFormatPainter();issue(error.isEmpty()?tr("Invalid or stale style snapshot. Copy formatting again."):error);return;}
        m_painterPending=false;m_painterRows=rows;m_painterOrigin=target;m_painterAction->setProperty("ready",true);for(auto *view:views)view->viewport()->setCursor(Qt::CrossCursor);m_frozenTop->focusCurrent();m_status->setText(tr("Format painter ready • Click a destination or drag a multiple of %1 × %2 cells • Escape cancels").arg(rows.size()).arg(rows.first().toArray().size()));
    });
}

bool MainWindow::formatPainterMouseEvent(QObject *object,QEvent *event) {
    if(event->type()!=QEvent::MouseButtonPress && event->type()!=QEvent::MouseButtonRelease && event->type()!=QEvent::MouseMove)return false;
    auto views=m_frozenTop->views();views.append(m_grid);bool viewport=false;for(auto *view:views)if(object==view->viewport())viewport=true;if(!viewport)return false;
    const auto *mouse=static_cast<QMouseEvent *>(event);if(event->type()==QEvent::MouseButtonPress && mouse->button()!=Qt::LeftButton){cancelFormatPainter();return false;}
    if(event->type()!=QEvent::MouseButtonPress && !m_painterDragging)return false;
    QModelIndex cell;for(auto *view:views){if(!view->isVisible())continue;const auto point=view->viewport()->mapFromGlobal(mouse->globalPosition().toPoint());if(view->viewport()->rect().contains(point)){cell=view->indexAt(point);break;}}
    if(event->type()==QEvent::MouseButtonPress){if(!cell.isValid())return true;m_painterAnchor=QPoint(cell.column()+1,cell.row()+1);m_painterDestination=targetContext();m_painterDragGeneration=m_navigationGeneration;m_painterDragging=true;}
    if(event->type()!=QEvent::MouseButtonRelease)return true;
    const auto rows=m_painterRows;const auto origin=m_painterOrigin,destination=m_painterDestination;const auto anchor=m_painterAnchor;const auto navigation=m_painterDragGeneration;cancelFormatPainter();
    bool editor=false;for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))editor|=delegate->hasEditor();
    if(!cell.isValid() || m_busy || m_unknown || m_info["readonly"].toBool() || m_model->projected() || !matchesTarget(destination) || origin["workbook"]!=m_info["id"] || origin["expected_state"]!=m_info["state"] || navigation!=m_navigationGeneration || m_source->isModified() || m_address->isModified() || editor){issue("Format painter inputs changed. No formatting applied.");return true;}
    const int r1=qMin(anchor.y(),cell.row()+1),c1=qMin(anchor.x(),cell.column()+1);int r2=qMax(anchor.y(),cell.row()+1),c2=qMax(anchor.x(),cell.column()+1);
    if(r1==r2 && c1==c2){r2+=rows.size()-1;c2+=rows.first().toArray().size()-1;}
    const auto edits=formatPainterEdits(rows,r1,c1,r2,c2);if(edits.isEmpty() || intersectsHidden(r1,c1,r2,c2)){issue("Choose a visible in-grid destination of at most 10,000 cells, with exact multiples of the copied pattern. No formatting applied.");return true;}
    auto command=destination;command["action"]="edit";command["cells"]=edits;command["label"]="Paint cell formatting";
    if(QJsonDocument(command).toJson(QJsonDocument::Compact).size()>4*1024*1024-4096){issue("Formatting request exceeds the ordinary request budget. Choose a smaller destination.");return true;}
    submit(command);return true;
}

void MainWindow::recordCommand() {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    const auto target=targetContext();const auto criteria=m_listSpec;const auto hidden=m_hiddenRows;captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target))return;
    QDialog dialog(this);dialog.setObjectName("recordCommandDialog");dialog.setWindowTitle(tr("Work with declared records"));QFormLayout layout(&dialog);QLineEdit name("Records",&dialog);name.setObjectName("recordCommandName");name.setMaxLength(64);QComboBox action(&dialog);action.setObjectName("recordCommandMode");
    const QList<QPair<QString,QString>> actions{{tr("Copy formulas"),"copy"},{tr("Paste all"),"all"},{tr("Paste values"),"values"},{tr("Paste formats"),"formats"},{tr("Fill down from first visible record"),"fill"},{tr("Clear contents"),"clear_contents"},{tr("Clear formats"),"clear_formats"},{tr("Clear all"),"clear_all"},{tr("Export XLSX formulas"),"xlsx_formulas"},{tr("Export XLSX values"),"xlsx_values"},{tr("Export ODS formulas"),"ods_formulas"},{tr("Export ODS values"),"ods_values"},{tr("Reconcile: retain positions"),"retain_positions"},{tr("Reconcile: regenerate IDs"),"regenerate"}};for(const auto &item:actions)action.addItem(item.first,item.second);
    QSpinBox first(&dialog),last(&dialog);first.setObjectName("recordFirstColumn");last.setObjectName("recordLastColumn");first.setRange(1,16384);last.setRange(1,16384);first.setValue(m_grid->currentIndex().column()+1);last.setValue(first.value());layout.addRow(tr("Declared list"),&name);layout.addRow(tr("Operation"),&action);layout.addRow(tr("First source column"),&first);layout.addRow(tr("Last source column"),&last);QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog);layout.addRow(&buttons);connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted || !matchesTarget(target) || criteria!=m_listSpec || hidden!=m_hiddenRows)return;
    if(first.value()>last.value()){issue("Choose ordered source columns.");return;}
    QJsonArray columns,excluded;for(int col=first.value();col<=last.value();++col)columns.append(col);for(int row:hidden)excluded.append(row+1);
    auto request=target;request.remove("workbook");request["id"]=name.text();request["columns"]=columns;request["exclude_rows"]=excluded;if(m_model->projected()){request["sort"]=criteria["sort"];request["filters"]=criteria["filters"];}
    const auto operation=action.currentData().toString();const auto clipboardGeneration=m_clipboardGeneration;
    m_client->request(base()+"/records/read",request,[this,target,criteria,hidden,request,operation,clipboardGeneration](QJsonObject review,QString error) mutable {
        if(!error.isEmpty()){issue(error);return;}if(!matchesTarget(target) || criteria!=m_listSpec || hidden!=m_hiddenRows || m_source->isModified())return;
        const auto spec=review["spec"].toObject();if(m_model->projected())for(const auto &key:QStringList{"r1","c1","r2","c2"})if(criteria[key]!=spec[key]){issue("The active projection does not match this declared list.");return;}
        QJsonArray ids;for(const auto &item:review["records"].toArray())ids.append(item.toObject()["id"]);
        const bool reconcile=operation=="retain_positions" || operation=="regenerate";if(!review["verified"].toBool() && !reconcile){issue("Declared identities are unverified. Reconcile explicitly before editing or copying.");return;}
        if(QMessageBox::question(this,tr("Review record operation"),tr("Apply %1 to %2 reviewed visible records in %3? Targets use stable IDs. Reconciliation does not replay skipped handlers.").arg(operation).arg(ids.size()).arg(request["id"].toString()),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel)!=QMessageBox::Yes || !matchesTarget(target) || criteria!=m_listSpec || hidden!=m_hiddenRows)return;
        if(reconcile){auto command=target;command["action"]="list_reconcile";command["id"]=request["id"];command["mode"]=operation;submit(command);return;}
        if(operation.contains('_') && !operation.startsWith("clear_")){
            const auto parts=operation.split('_');const auto path=QFileDialog::getSaveFileName(this,tr("Export declared records"),{},parts[0].toUpper()+" (*."+parts[0]+")");if(path.isEmpty() || !matchesTarget(target) || sameFile(path,m_info["path"].toString()))return;
            const auto native=m_info["path"].toString();
            request["mode"]=parts[1];
            m_client->request(base()+"/records/export/"+parts[0],request,[this,target,path,native](QJsonObject result,QString issueText){
                if(!issueText.isEmpty()){issue(issueText);return;}
                if(!matchesTarget(target) || result["state"]!=target["expected_state"])return;
                QStringList report;for(const auto &line:result["report"].toArray())report.append(line.toString());
                QMessageBox review(QMessageBox::Question,tr("Review declared-record export"),tr("Save this projection snapshot?"),QMessageBox::Yes|QMessageBox::Cancel,this);
                review.setObjectName("recordExportReview");review.setOption(QMessageBox::Option::DontUseNativeDialog);
                review.setTextFormat(Qt::PlainText);review.setDefaultButton(QMessageBox::Cancel);review.setDetailedText(report.join('\n'));
                if(review.exec()!=QMessageBox::Yes || !matchesTarget(target) || m_source->isModified())return;
                QString failure;
                if(!ExportWriter::save(path,native,result,failure))issue(failure);
                else m_status->setText("Declared records exported • Native IDs retained in workbook");
            });return;
        }
        auto apply=[this,target,request,ids](QJsonArray rows,const QString &mode,bool repeat=false,const QString &scope=QString()){auto command=target;command["action"]="list_edit";command["id"]=request["id"];command["record_ids"]=ids;command["columns"]=request["columns"];command["mode"]=mode;command["rows"]=rows;command["repeat"]=repeat;if(!scope.isEmpty())command["clear_scope"]=scope;command["label"]="Edit reviewed records";submit(command);};
        if(operation.startsWith("clear_")){apply({},"clear",false,operation.mid(6));return;}
        if(operation=="all" || operation=="values" || operation=="formats"){
            QJsonObject payload;QString failure;
            if(!ClipboardData::decode(QApplication::clipboard()->mimeData(),payload,failure,3*1024*1024)){issue(failure);return;}
            if(payload["cut"].toBool()){issue("Use Copy for record-target paste.");return;}
            apply(payload["rows"].toArray(),operation);return;
        }
        request["include_header"]=false;request["mode"]="all";m_client->request(base()+"/records/copy",request,[this,target,criteria,hidden,request,operation,clipboardGeneration,apply](QJsonObject result,QString error){if(!error.isEmpty()){issue(error);return;}if(!matchesTarget(target) || criteria!=m_listSpec || hidden!=m_hiddenRows || m_source->isModified() || result["state"]!=target["expected_state"])return;const auto rows=result["rows"].toArray();if(operation=="fill"){if(!rows.isEmpty())apply(QJsonArray{rows.first()},"all",true);return;}if(m_clipboardGeneration!=clipboardGeneration)return;QJsonObject payload{{"rows",rows},{"origin_row",1},{"origin_col",1},{"record_projection",true},{"cut",false}};QStringList lines;for(const auto &line:rows){QStringList fields;for(const auto &cell:line.toArray())fields.append(cell.toObject()["display"].toString());lines.append(fields.join('\t'));}auto *mime=new QMimeData;mime->setText(lines.join('\n'));mime->setData("application/x-datahorizon-cells",QJsonDocument(payload).toJson(QJsonDocument::Compact));QApplication::clipboard()->setMimeData(mime);m_status->setText("Copied declared record formulas with source coordinates");});
    });
}

void MainWindow::declareRecords() {
    if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool() || m_model->projected())return;
    auto range=selectedRange();if(range.isEmpty())return;const auto target=targetContext(range);captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target))return;
    QDialog dialog(this);dialog.setWindowTitle(tr("Declare persistent list records"));dialog.setObjectName("declareRecordsDialog");QFormLayout layout(&dialog);QLineEdit name(&dialog);name.setObjectName("recordListName");name.setMaxLength(64);name.setText("Records");QCheckBox header(tr("First row is a header"),&dialog);layout.addRow(tr("List name"),&name);layout.addRow(&header);QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog);layout.addRow(&buttons);connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted || !matchesTarget(target) || selectedRange()!=range)return;
    range["header"]=header.isChecked();auto request=target;request["action"]="list_declare";request["id"]=name.text();request["spec"]=range;request["label"]="Declare persistent list";submit(request);
}

void MainWindow::operationJobs() {
    if(m_jobClosing || m_info.isEmpty() || m_unknown || (m_busy && m_activeOperationJob.isEmpty()))return;
    captureGridDraft(false);if(!resolveDraft())return;
    const auto resource=base()+"/operation-jobs",workbook=m_info["id"].toString();
    const auto session=m_client->generation();
    QDialog dialog(this);dialog.setWindowTitle(tr("Reviewed operation jobs"));dialog.setObjectName("operationJobsDialog");dialog.resize(780,620);QFormLayout layout(&dialog);layout.setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
    QLabel explanation(tr("Prepare first, review the sealed plan, then execute. Batches commit separately. Cancellation keeps committed batches. Journals remain beside the native file for explicit recovery."),&dialog);explanation.setWordWrap(true);layout.addRow(&explanation);
    QComboBox mode(&dialog);mode.setObjectName("operationJobMode");mode.addItem(tr("Stage CSV — explicit batches, Text columns"),"csv");mode.addItem(tr("Find in stored source"),"find");mode.addItem(tr("List rows, ascending first column"),"list");
    QLineEdit identifier(m_activeOperationJob.isEmpty()?QUuid::createUuid().toString(QUuid::WithoutBraces):m_activeOperationJob,&dialog),source(&dialog);identifier.setObjectName("operationJobId");source.setObjectName("operationJobSource");identifier.setMaxLength(64);
    QSpinBox rows(&dialog);rows.setObjectName("operationJobRows");rows.setRange(1,100000);rows.setValue(10000);layout.addRow(tr("Preparation"),&mode);layout.addRow(tr("Job ID (keep for recovery)"),&identifier);layout.addRow(tr("CSV path or find Text"),&source);layout.addRow(tr("Read rows from A1 (one column)"),&rows);
    QPlainTextEdit result(&dialog);result.setObjectName("operationJobReport");result.setReadOnly(true);result.setAccessibleName(tr("Job plan, progress and durable receipts"));layout.addRow(&result);
    QWidget controls(&dialog);QHBoxLayout buttons(&controls);QPushButton prepare(tr("Prepare"),&controls),inspect(tr("Inspect / retry status"),&controls),start(tr("Execute / resume…"),&controls),cancel(tr("Cancel job"),&controls),release(tr("Release journal"),&controls),next(tr("Next page"),&controls),close(tr("Close review"),&controls);prepare.setObjectName("prepareOperationJob");inspect.setObjectName("inspectOperationJob");start.setObjectName("startOperationJob");cancel.setObjectName("cancelOperationJob");close.setObjectName("closeOperationJob");for(auto *button:{&prepare,&inspect,&start,&cancel,&release,&next,&close})buttons.addWidget(button);layout.addRow(&controls);
    QJsonObject plan;QTimer timer(&dialog);timer.setInterval(150);QPointer<QDialog> guard(&dialog);bool requesting=false;int offset=0;
    auto show=[&,guard](QJsonObject value,QString error){
        if(!guard || session!=m_client->generation() || m_info["id"].toString()!=workbook)return;requesting=false;
        if(!error.isEmpty()){result.setPlainText(tr("Status unavailable. Keep job ID %1 and inspect again. A lost reply does not prove rollback.\n%2").arg(identifier.text(),error));timer.stop();start.setEnabled(false);return;}
        plan=value;if(m_activeOperationJob==identifier.text())m_activeJobStatus=value;
        result.setPlainText(QString::fromUtf8(QJsonDocument(value).toJson(QJsonDocument::Indented)));const auto status=value["status"].toString();const bool active=status=="running" || status=="staging";
        identifier.setEnabled(!active && m_activeOperationJob.isEmpty());next.setEnabled(value["has_more"].toBool());
        prepare.setEnabled(!active && m_activeOperationJob.isEmpty());start.setEnabled(!active && !value["plan_digest"].toString().isEmpty() && status!="completed");
        if(active)timer.start();else timer.stop();
        if(!active && status!="unknown" && status!="interrupted" && m_activeOperationJob==identifier.text()){m_activeOperationJob.clear();refresh();}
    };
    auto inspectJob=[&,guard,show]{if(!guard || requesting)return;requesting=true;m_client->request(resource+"/"+identifier.text()+"/status",{{"offset",offset}},show);};
    connect(&timer,&QTimer::timeout,&dialog,inspectJob);connect(&inspect,&QPushButton::clicked,&dialog,inspectJob);
    connect(&prepare,&QPushButton::clicked,&dialog,[&,guard,show]{if(requesting || !m_activeOperationJob.isEmpty())return;auto request=QJsonObject{{"job_id",identifier.text()},{"expected_state",m_info["state"]},{"sheet",m_model->sheet()}};if(mode.currentData()=="csv"){if(m_info["readonly"].toBool())return;request["action"]="edit";request["mode"]="batches";request["source_path"]=source.text();}else{request["action"]=mode.currentData().toString();request["r2"]=rows.value();request["c2"]=1;if(mode.currentData()=="find")request["needle"]=source.text();else request["sort"]=QJsonArray{QJsonObject{{"col",1}}};}requesting=true;m_client->request(resource,request,show);});
    connect(&start,&QPushButton::clicked,&dialog,[&,guard,show]{
        if(requesting || plan["plan_digest"].toString().isEmpty() || plan["job_id"].toString()!=identifier.text())return;
        const auto reviewed=plan;const auto job=identifier.text();requesting=true;timer.stop();
        m_client->request(base(),{},[&,guard,show,reviewed,job](QJsonObject current,QString error){
            if(!guard || session!=m_client->generation() || m_info["id"].toString()!=workbook)return;
            requesting=false;
            if(!error.isEmpty()){show({},error);return;}
            if(current["id"].toString()!=workbook || current["workbook_id"]!=m_info["workbook_id"] || !current["state"].isString() || job!=identifier.text() || plan!=reviewed){issue("Job review context changed; inspect again.");return;}
            if(current["readonly"].toBool() && reviewed["action"]=="edit"){issue("Open the native file writable before executing an edit job.");return;}
            const bool resume=reviewed["status"]!="prepared" || reviewed["expected_state"]!=current["state"];
            const auto localState=m_info["state"];
            QMessageBox review(QMessageBox::Question,resume?tr("Resume against current inputs?"):tr("Execute reviewed job?"),
                tr("Job %1: %2 targets; %3 mode.\n%4 batches already committed; %5 targets remain. Cancellation retains commits.\n\nOriginal state: %6\nCurrent state: %7\n\n%8")
                    .arg(job).arg(reviewed["total"].toInt()).arg(reviewed["mode"].toString()).arg(reviewed["committed_batches"].toInt())
                    .arg(reviewed["total"].toInt()-reviewed["next"].toInt()).arg(reviewed["expected_state"].toString(),current["state"].toString(),resume?tr("Explicitly resume this sealed plan against the current inputs?"):tr("Execute this unchanged sealed plan?")),
                QMessageBox::Yes|QMessageBox::Cancel,&dialog);
            review.setObjectName("operationJobExecutionReview");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setTextFormat(Qt::PlainText);review.setDefaultButton(QMessageBox::Cancel);
            review.setDetailedText(QString::fromUtf8(QJsonDocument(reviewed).toJson(QJsonDocument::Indented)));
            const auto choice=review.exec();
            if(choice!=QMessageBox::Yes || !guard || session!=m_client->generation() || m_info["id"].toString()!=workbook || m_info["state"]!=localState || job!=identifier.text() || plan!=reviewed)return;
            QJsonObject request{{"expected_state",current["state"]},{"plan_digest",reviewed["plan_digest"]}};
            if(reviewed.contains("execution_id"))request["execution_id"]=reviewed["execution_id"];
            m_activeOperationJob=job;m_activeJobStatus=reviewed;setBusy(true,tr("Job running • keep ID %1").arg(job));requesting=true;
            m_client->request(resource+"/"+job+(resume?"/resume":"/start"),request,show);
        },false);
    });
    connect(&cancel,&QPushButton::clicked,&dialog,[&,guard,show]{if(!guard || requesting)return;requesting=true;m_client->request(resource+"/"+identifier.text()+"/cancel",{},show);});
    connect(&next,&QPushButton::clicked,&dialog,[&]{if(!requesting){offset+=100;inspectJob();}});
    connect(&release,&QPushButton::clicked,&dialog,[&,guard,show]{if(requesting || !m_activeOperationJob.isEmpty())return;requesting=true;m_client->request(resource+"/"+identifier.text()+"/release",{},show);});
    connect(&identifier,&QLineEdit::textChanged,&dialog,[&]{offset=0;plan={};});
    connect(&close,&QPushButton::clicked,&dialog,&QDialog::accept);
    if(!m_activeOperationJob.isEmpty())QTimer::singleShot(0,&dialog,inspectJob);
    connect(m_client,&RestClient::configured,&dialog,&QDialog::reject);
    dialog.exec();timer.stop();
}

void MainWindow::calculateSelection() {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    auto range=selectedRange();if(range.isEmpty())return;const auto target=targetContext(range);const auto service=m_client->url();const auto generation=++m_calculationGeneration;
    m_model->setBackgroundEnabled(false);
    auto request=range;request["sheet"]=m_model->sheet();request["expected_state"]=m_info["state"];request["job_id"]=QUuid::createUuid().toString(QUuid::WithoutBraces);
    const auto resource=base()+"/calculation/jobs/"+request["job_id"].toString();
    auto *progress=new QProgressDialog(tr("Calculating stored selection…"),tr("Cancel"),0,0,this);progress->setObjectName("backgroundCalculationProgress");progress->setWindowModality(Qt::NonModal);progress->setMinimumDuration(0);progress->show();
    auto *timer=new QTimer(progress);timer->setInterval(100);
    connect(progress,&QProgressDialog::canceled,this,[this,resource]{m_client->request(resource+"/cancel",{},[](QJsonObject,QString){});});
    auto finish=[this,target,service,generation,resource,progress,timer](QJsonObject result,QString error){
        if(error.isEmpty() && result["status"]=="running"){progress->setLabelText(result["phase"].toString());timer->start();return;}
        timer->stop();progress->hide();progress->deleteLater();m_client->request(resource+"/release",{},[](QJsonObject,QString){});
        if(service==m_client->url() && generation==m_calculationGeneration)m_model->setBackgroundEnabled(true);
        if(service!=m_client->url() || generation!=m_calculationGeneration || !matchesTarget(target))return;
        if(!error.isEmpty()){issue(error);return;}const auto status=result["status"].toString();
        if(status=="completed"){const auto snapshot=result["result"].toObject();int errors=0;for(const auto &cell:snapshot["cells"].toArray())errors+=cell.toObject()["calculation"]=="error";m_status->setText(tr("Background snapshot: %1 cells, %2 formula errors • UTC %3").arg(snapshot["cells"].toArray().size()).arg(errors).arg(result["clock"].toString()));}
        else m_status->setText(tr("Background calculation: %1").arg(status));
    };
    connect(timer,&QTimer::timeout,this,[this,timer,resource,finish]{timer->stop();m_client->request(resource,{},finish,false);});
    m_client->request(base()+"/calculation/jobs",request,finish);
}

void MainWindow::enterLocalized() {
    if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool())return;const auto current=QPersistentModelIndex(m_grid->currentIndex());if(!current.isValid())return;
    const int row=m_model->sourceRow(current.row())+1,col=current.column()+1;const auto target=targetContext(QJsonObject{{"r1",row},{"r2",row},{"c1",col},{"c2",col}});captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target))return;
    QDialog dialog(this);dialog.setObjectName("localizedEntryDialog");dialog.setWindowTitle(tr("Enter localized value"));QFormLayout layout(&dialog);QLineEdit source(&dialog);source.setObjectName("localizedSource");source.setMaxLength(65536);QComboBox kind(&dialog),profile(&dialog);kind.setObjectName("localizedKind");profile.setObjectName("localizedProfile");for(const auto &value:QStringList{"number","date","time","datetime","timestamp_utc"})kind.addItem(value,value);for(const auto &value:QStringList{"invariant","en_US","de_DE","fr_FR"})profile.addItem(value,value);layout.addRow(tr("Source value"),&source);layout.addRow(tr("Type"),&kind);layout.addRow(tr("Conversion profile"),&profile);QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog);layout.addRow(&buttons);connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted || !matchesTarget(target) || m_grid->currentIndex()!=current)return;
    m_client->request("/v1/convert/value",{{"source",source.text()},{"kind",kind.currentData().toString()},{"locale",profile.currentData().toString()}},[this,target,current,row,col](QJsonObject result,QString error){if(!error.isEmpty()){issue(error);return;}if(!matchesTarget(target) || m_grid->currentIndex()!=current || m_source->isModified())return;QStringList report;for(const auto &line:result["report"].toArray())report.append(line.toString());QMessageBox review(QMessageBox::Question,tr("Review value conversion"),tr("Store %1 as %2?").arg(result["cell"].toObject()["source"].toString(),result["cell"].toObject()["kind"].toString()),QMessageBox::Yes|QMessageBox::Cancel,this);review.setDetailedText(report.join('\n'));if(review.exec()!=QMessageBox::Yes || !matchesTarget(target) || m_grid->currentIndex()!=current || m_source->isModified())return;auto cell=result["cell"].toObject();cell["row"]=row;cell["col"]=col;auto command=target;command["action"]="edit";command["cells"]=QJsonArray{cell};command["label"]="Enter localized value";submit(command);});
}

void MainWindow::enterTemporal() {
    if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool() || m_address->isModified())return;
    const auto current=QPersistentModelIndex(m_grid->currentIndex());if(!current.isValid())return;
    const auto selection=m_grid->selectionModel()->selection();const auto service=m_client->url();
    const QJsonObject range{{"r1",m_model->sourceRow(current.row())+1},{"r2",m_model->sourceRow(current.row())+1},{"c1",current.column()+1},{"c2",current.column()+1}};
    const auto target=targetContext(range);captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target))return;
    QDialog dialog(this);dialog.setObjectName("temporalEntryDialog");dialog.setWindowTitle(tr("Enter date/time"));QFormLayout layout(&dialog);
    QComboBox kind(&dialog);kind.setObjectName("temporalKind");kind.addItem(tr("Date"),"date");kind.addItem(tr("Time"),"time");kind.addItem(tr("Date and time"),"datetime");layout.addRow(tr("Type"),&kind);
    QLineEdit source(&dialog);source.setObjectName("temporalSource");source.setMaxLength(32);source.setText(m_model->cell(current)["source"].toString());layout.addRow(tr("ISO value"),&source);
    QLabel help(tr("Date: YYYY-MM-DD\nTime: HH:MM:SS[.ffffff]\nDate and time: YYYY-MM-DDTHH:MM:SS[.ffffff]\nCivil values without timezone offsets."),&dialog);layout.addRow(&help);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog);layout.addRow(&buttons);connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted)return;
    if(m_busy || m_unknown || service!=m_client->url() || !matchesTarget(target) || m_grid->currentIndex()!=current || m_grid->selectionModel()->selection()!=selection || m_source->isModified() || m_address->isModified()){issue("Date/time target changed. Open entry again.");return;}
    auto command=target;command["action"]="edit";command["label"]="Enter date/time";command["cells"]=QJsonArray{QJsonObject{{"row",range["r1"]},{"col",range["c1"]},{"source",source.text()},{"kind",kind.currentData().toString()}}};submit(command);
}

void MainWindow::showFormatCells() {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    if(m_info["readonly"].toBool()){issue("Workbook is read-only. Formatting is unavailable.");return;}
    if(m_address->isModified()){issue("Finish or cancel address entry before formatting.");return;}
    auto range=selectedRange();if(range.isEmpty())return;const auto target=targetContext(range);const auto selection=m_grid->selectionModel()->selection();const auto current=QPersistentModelIndex(m_grid->currentIndex());const auto service=m_client->url();
    captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target))return;
    const auto first=SheetModel::columnName(range["c1"].toInt())+QString::number(range["r1"].toInt()),last=SheetModel::columnName(range["c2"].toInt())+QString::number(range["r2"].toInt());FormatCellsDialog dialog(first+(first==last?QString():":"+last),this);
    if(dialog.exec()!=QDialog::Accepted)return;
    bool editor=false;auto views=m_frozenTop->views();views.append(m_grid);for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))editor|=delegate->hasEditor();
    if(m_busy || m_unknown || service!=m_client->url() || !matchesTarget(target) || m_grid->currentIndex()!=current || m_grid->selectionModel()->selection()!=selection || m_source->isModified() || m_address->isModified() || editor){issue("Formatting target changed. Review the selection and open Format Cells again.");return;}
    const auto style=dialog.patch();if(style.isEmpty() && !dialog.replaceStyle())return;format(style,target,dialog.replaceStyle());
}

void MainWindow::adjustDecimals(int change) {
    if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool())return;
    if(m_address->isModified() || !m_model->loaded(m_grid->currentIndex())){issue("Finish address entry and wait for the active cell before adjusting decimals.");return;}
    auto range=selectedRange();if(range.isEmpty())return;const auto target=targetContext(range);const auto current=QPersistentModelIndex(m_grid->currentIndex());const auto style=m_model->cell(current)["style"].toObject();const auto mode=style["format"].toString();const bool configurable=QStringList{"number","percent","currency"}.contains(mode) || (mode.isEmpty() && (style.contains("decimals") || style.contains("grouping")));const int places=configurable?style["decimals"].toInt(2):2;const int next=qBound(0,places+change,15);
    captureGridDraft(false);if(!resolveDraft() || !matchesTarget(target) || current!=m_grid->currentIndex())return;
    format({{"decimals",next}},target);
}

void MainWindow::format(const QJsonObject &style, QJsonObject target, bool replaceStyle) {
    if (m_busy) return;
    if (target.isEmpty()) {
        target=selectedRange(); if (target.isEmpty()) return;
        target=targetContext(target);
    }
    QJsonArray edits;
    for (int row = target["r1"].toInt(); row <= target["r2"].toInt(); ++row)
        for (int col = target["c1"].toInt(); col <= target["c2"].toInt(); ++col)
            edits.append(QJsonObject{{"row", row}, {"col", col}, {"style", style}, {"replace_style",replaceStyle}});
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
    if(m_model->projected()){if(cut)issue("Cut is unavailable in a list view. Copy list values or clear the list first.");else copyListValues(true);return;}
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

QJsonObject MainWindow::listValuesRequest(bool selectionOnly) {
    if(!m_model->projected() || m_listSpec.isEmpty()){issue("Apply a list view first.");return {};}
    auto request=targetContext(m_listSpec);const auto spec=m_listSpec;
    if(selectionOnly){
        const auto ranges=m_grid->selectionModel()->selection();if(ranges.size()!=1){issue("Select one rectangle of list values.");return {};}
        const auto range=ranges.first();
        if(range.left()+1<spec["c1"].toInt() || range.right()+1>spec["c2"].toInt()){issue("Select columns inside the list source scope.");return {};}
        request["view_r1"]=range.top()+1;request["view_r2"]=range.bottom()+1;request["select_c1"]=range.left()+1;request["select_c2"]=range.right()+1;
    }
    QJsonArray rows,columns;
    for(int row:m_hiddenRows)if(row+1>=spec["r1"].toInt() && row+1<=spec["r2"].toInt())rows.append(row+1);
    for(int col:m_hiddenColumns)if(col+1>=spec["c1"].toInt() && col+1<=spec["c2"].toInt())columns.append(col+1);
    request["exclude_rows"]=rows;request["exclude_columns"]=columns;return request;
}

void MainWindow::copyListValues(bool selectionOnly) {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    const auto target=listValuesRequest(selectionOnly);if(target.isEmpty())return;
    const auto criteria=m_listSpec;const auto hiddenRows=m_hiddenRows,hiddenColumns=m_hiddenColumns;
    captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(target) || !m_model->projected() || m_listSpec!=criteria)return;
    const auto generation=++m_clipboardGeneration;const auto service=m_client->url();
    auto request=target;request.remove("workbook");
    m_client->request(base()+"/list/values",request,[this,target,criteria,hiddenRows,hiddenColumns,generation,service](QJsonObject result,QString error){
        if(service!=m_client->url() || generation!=m_clipboardGeneration || !matchesTarget(target) || !m_model->projected() || m_listSpec!=criteria || m_hiddenRows!=hiddenRows || m_hiddenColumns!=hiddenColumns)return;
        if(!error.isEmpty()){issue(error);return;}if(result["state"]!=target["expected_state"]){issue("List inputs changed; refresh before copying.");return;}
        const auto rows=result["rows"].toArray();if(rows.isEmpty() || rows.first().toArray().isEmpty()){issue("No visible list values to copy. Clipboard retained.");return;}
        QStringList lines;for(const auto &value:rows){QStringList fields;for(const auto &cell:value.toArray()){
            auto text=cell.toObject()["display"].toString();if(text.contains('\t') || text.contains('\n') || text.contains('\r') || text.contains('"')){text.replace("\"","\"\"");text='"'+text+'"';}fields.append(text);
        }lines.append(fields.join('\t'));}
        const auto text=lines.join('\n');
        const auto bytes=QJsonDocument(QJsonObject{{"rows",rows},{"origin_row",1},{"origin_col",1},{"cut",false},{"list_values",true}}).toJson(QJsonDocument::Compact);
        if(bytes.size()+text.toUtf8().size()>4*1024*1024){issue("List clipboard exceeds 4 MiB. Narrow the source scope.");return;}
        auto *mime=new QMimeData;mime->setText(text);mime->setData("application/x-datahorizon-cells",bytes);QApplication::clipboard()->setMimeData(mime);
        m_status->setText(tr("Copied %1 visible list rows in display order • Values and styles; formulas converted to values").arg(rows.size()));
    });
}

void MainWindow::paste(bool valuesOnly, bool formatsOnly) {
    if (m_busy) return;
    if (m_model->projected()) { issue("Paste into a sorted or filtered projection is unavailable. Clear the list view first."); return; }
    if (m_source->hasFocus()) { m_source->paste(); return; }
    const auto *mime = QApplication::clipboard()->mimeData();
    if (!mime || !m_grid->currentIndex().isValid()) return;
    QJsonObject command;
    constexpr int maxCells=ClipboardData::maxCells;QString failure;
    if(!ClipboardData::decode(mime,command,failure)){issue(failure);return;}
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
    else {
        QJsonObject schema;QFile companion(path+".dh-types.json");
        if(companion.exists()) {if(!companion.open(QIODevice::ReadOnly) || companion.size()>1024*1024){issue("CSV temporal companion must be readable and at most 1 MiB.");return;}QJsonParseError error;const auto doc=QJsonDocument::fromJson(companion.read(1024*1024+1),&error);if(error.error!=QJsonParseError::NoError || !doc.isObject()){issue("Invalid CSV temporal companion JSON.");return;}schema=doc.object();}
        importCsvOptions(bytes,target,extension=="tsv"?"\t":",",schema);
    }
}

void MainWindow::importCsvOptions(const QByteArray &bytes, QJsonObject target, const QString &defaultDelimiter, QJsonObject temporalSchema) {
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
    QCheckBox restore(tr("Restore date/time types from the companion file"),&dialog);restore.setChecked(!temporalSchema.isEmpty());restore.setVisible(!temporalSchema.isEmpty());layout.addRow(&restore);
    QLabel note(tr("Choose the file's encoding. Invalid bytes stop the import. Review parsed Text cells before applying."),&dialog);
    note.setWordWrap(true); layout.addRow(&note);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog); layout.addRow(&buttons);
    connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);
    connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted) return;
    if (!matchesTarget(target) || m_model->projected()) { issue("CSV import target changed; import cancelled."); return; }
    importCsvBytes(bytes,encoding.currentData().toString(),delimiter.currentData().toString(),target,restore.isChecked()?temporalSchema:QJsonObject{});
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

void MainWindow::importCsvBytes(const QByteArray &bytes, const QString &encoding, const QString &delimiter, QJsonObject target, QJsonObject temporalSchema) {
    if(bytes.size()>3*1024*1024) { issue("CSV source exceeds 3 MiB."); return; }
    auto request=QJsonObject{{"csv_base64",QString::fromLatin1(bytes.toBase64())},{"encoding",encoding},{"delimiter",delimiter}};if(!temporalSchema.isEmpty())request["temporal_schema"]=temporalSchema;previewCsv(request,target);
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
        QString summary=tr("%1 converted values; %2 conversion errors.\n%3 ambiguous slash dates use your selected date order. Dates/times retain native ISO types.\n\nPreview (first 2 rows / 4 columns, 40 characters per field):\n%4\n\nOpen Details for encoding, column types, conversion issues and more preview rows.")
            .arg(preview["converted_cells"].toInt()).arg(preview["conversion_errors"].toInt())
            .arg(preview["ambiguous_dates"].toInt()).arg(shortLines.join('\n'));
        review.setInformativeText(summary);
        review.setDetailedText(report.join('\n')+tr("\n\nPreview (first 8 rows/columns, 120 characters per field):\n")+lines.join('\n'));
        review.button(QMessageBox::Yes)->setText(tr("Import"));
        auto *typesButton=review.addButton(tr("Column types…"),QMessageBox::ActionRole); typesButton->setObjectName("csvTypesButton");
        typesButton->setEnabled(preview["columns"].toInt()>0 && !request.contains("temporal_schema"));
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
    type.addItem(tr("Boolean (TRUE/FALSE)"),"boolean"); type.addItem(tr("Date YYYY-MM-DD → native date"),"date_iso");
    type.addItem(tr("Date M/D/YYYY → native date"),"date_mdy"); type.addItem(tr("Date D/M/YYYY → native date"),"date_dmy"); type.addItem(tr("Time HH:MM:SS"),"time_iso"); type.addItem(tr("Datetime YYYY-MM-DDTHH:MM:SS"),"datetime_iso");type.addItem(tr("Date in conversion profile"),"date_locale");type.addItem(tr("Datetime in conversion profile"),"datetime_locale");type.addItem(tr("Timestamp normalized to UTC"),"timestamp_utc");
    QCheckBox header(tr("First record is a header; keep it as Text"),&dialog); header.setObjectName("csvHeader");
    header.setChecked(request["header"].toBool());
    QComboBox profile(&dialog);profile.setObjectName("csvLocaleProfile");for(const auto &value:QStringList{"invariant","en_US","de_DE","fr_FR"})profile.addItem(value,value);profile.setCurrentIndex(qMax(0,profile.findData(request["locale"].toString("invariant"))));layout.addRow(tr("Explicit conversion profile"),&profile);
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
    QLabel note(tr("Unspecified columns remain Text. Dates/times retain native ISO types. Empty converted fields become blank. Invalid conversions block Apply; review the result before importing."),&dialog);
    note.setWordWrap(true); layout.addRow(&note);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog); layout.addRow(&buttons);
    connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept); connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    load();
    if(dialog.exec()!=QDialog::Accepted) return false;
    request["column_types"]=types; request["header"]=header.isChecked();request["locale"]=profile.currentData().toString(); return true;
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
        QString failure;if(!saveCsvSnapshot(path,result,workbookPath,failure))issue(failure);
        else m_status->setText(result.contains("temporal_schema")?"CSV and date/time companion exported":"CSV exported • Native workbook retained");
    });
}

void MainWindow::saveNamedListView(const QString &name,bool replace) {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    if(!m_model->projected() || m_listSpec.isEmpty()){issue("Apply a list view before saving its criteria.");return;}
    if(!validListViewName(name)){issue("View names need 1–64 printable characters without surrounding spaces.");return;}
    if(m_namedViews.contains(name) && !replace){issue("A view with this name already exists. Choose another name or explicitly replace it.");return;}
    QJsonObject scope;if(!normalizedListView(m_listSpec,scope)){issue("These list criteria cannot be saved by the desktop tier.");return;}
    const auto target=targetContext();const auto criteria=m_listSpec;captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(target) || !m_model->projected() || criteria!=m_listSpec)return;
    auto views=m_namedViews;views[name]=scope;if(views.size()>16 || QJsonDocument(views).toJson(QJsonDocument::Compact).size()>128*1024){issue("Saved views exceed 16 names or 128 KiB total. Remove a view or narrow its criteria.");return;}
    m_namedViews=views;saveViewState();m_status->setText("Named list view saved on this computer • Source coordinates; criteria only");
}

void MainWindow::applyNamedListView(const QString &name) {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    QJsonObject scope;if(!m_namedViews.contains(name) || !normalizedListView(m_namedViews.value(name).toObject(),scope)){issue("Saved list view is unavailable or invalid.");return;}
    scope=targetContext(scope);const auto saved=m_namedViews.value(name);const auto key=m_viewKey;const auto service=m_client->url();
    captureGridDraft(false);if(!resolveDraft())return;
    if(service!=m_client->url() || m_busy || !matchesTarget(scope) || key!=m_viewKey || saved!=m_namedViews.value(name))return;
    ListCriteriaDialog dialog(scope,this);dialog.setWindowTitle(tr("Apply saved view: %1 (review source coordinates)").arg(name));
    if(dialog.exec()==QDialog::Accepted && service==m_client->url() && !m_busy && key==m_viewKey && matchesTarget(scope) && saved==m_namedViews.value(name))showList(dialog.scope());
}

void MainWindow::removeNamedListView(const QString &name) {
    if(m_busy || m_unknown || m_info.isEmpty() || !m_namedViews.contains(name))return;
    m_namedViews.remove(name);saveViewState();m_status->setText("Named list view deleted on this computer • Current projection retained");
}

void MainWindow::namedListDialog(int action) {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    const auto target=targetContext();const auto key=m_viewKey;const auto views=m_namedViews;bool ok=false;QString name;
    if(action==0) {
        if(!m_model->projected()){issue("Apply a list view before saving it.");return;}
        name=QInputDialog::getText(this,tr("Save named list view on this computer"),tr("Name (source coordinates and criteria; at most 16 views per sheet)"),QLineEdit::Normal,{},&ok).trimmed();
    } else {if(views.isEmpty()){issue("No named list views saved for this sheet on this computer.");return;}name=QInputDialog::getItem(this,action==1?tr("Apply named list view"):tr("Delete named list view"),tr("Saved view"),views.keys(),0,false,&ok);}
    if(!ok || name.isEmpty() || m_busy || !matchesTarget(target) || m_viewKey!=key || m_namedViews!=views)return;
    if(action==0){bool replace=false;if(views.contains(name)){replace=QMessageBox::question(this,tr("Replace named view"),tr("Replace the saved criteria for %1?").arg(name),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel)==QMessageBox::Yes;if(!replace || !matchesTarget(target) || key!=m_viewKey || views!=m_namedViews)return;}saveNamedListView(name,replace);}
    else if(action==1)applyNamedListView(name);else removeNamedListView(name);
}

void MainWindow::listDialog() {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    auto scope=m_model->projected()?m_listSpec:selectedRange();if(scope.isEmpty())return;
    if(!m_model->projected())scope["header"]=true;
    const int r1=scope["r1"].toInt(1),c1=scope["c1"].toInt(1);
    scope["r1"]=r1;scope["c1"]=c1;scope["r2"]=scope["r2"].toInt(r1);scope["c2"]=scope["c2"].toInt(c1);
    scope=targetContext(scope);captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(scope)){issue("List inputs changed. Review the source scope again.");return;}
    for(const auto &value:scope["filters"].toArray())if(value.toObject()["value"].isString() && value.toObject()["value"].toString().toUtf8().size()>65536){issue("This filter value exceeds the desktop editor's 64 KiB limit. Clear the list or manage its criteria through REST.");return;}
    ListCriteriaDialog dialog(scope,this);if(dialog.exec()==QDialog::Accepted)showList(dialog.scope());
}

void MainWindow::updateListSummary() {
    if(!m_listSummary)return;const bool visible=!m_busy && m_model->projected() && !m_listSpec.isEmpty();m_listSummary->setVisible(visible);if(!visible)return;
    const auto spec=m_listSpec;QStringList keys,filters;
    for(const auto &value:spec["sort"].toArray()){const auto key=value.toObject();keys.append(SheetModel::columnName(key["col"].toInt())+(key["descending"].toBool()?QString("↓"):QString("↑")));}
    for(const auto &value:spec["filters"].toArray()) {
        const auto filter=value.toObject();const QJsonValue scalar=filter.contains("value")?filter["value"]:QJsonValue(QJsonValue::Null);
        const QString type=filter.contains("value_kind")?filter["value_kind"].toString():scalar.isNull()?"NULL":scalar.isBool()?"Boolean":scalar.isDouble()?"Number":"Text";
        const auto encoded=QString::fromUtf8(QJsonDocument(QJsonArray{scalar}).toJson(QJsonDocument::Compact));
        filters.append(SheetModel::columnName(filter["col"].toInt())+" "+filter["op"].toString()+" "+type+" "+encoded.mid(1,encoded.size()-2).left(120));
    }
    const auto range=tr("%1%2:%3%4").arg(SheetModel::columnName(spec["c1"].toInt())).arg(spec["r1"].toInt()).arg(SheetModel::columnName(spec["c2"].toInt())).arg(spec["r2"].toInt());
    const auto text=tr("List %1 • Header %2 • Sort %3 • Filters (AND): %4").arg(range,spec["header"].toBool()?tr("yes"):tr("no"),keys.isEmpty()?tr("source order"):keys.join(", "),filters.isEmpty()?tr("none"):filters.join("; "));
    m_listSummary->setText(text);m_listSummary->setToolTip(text+tr("\nValues are previewed to 120 characters. Open Sort/filter to inspect or edit full criteria. Local hiding is separate from these source criteria."));
}

void MainWindow::showList(QJsonObject scope) {
    if (m_busy || m_unknown) return;
    const auto defaults=targetContext();
    for (auto field=defaults.begin(); field!=defaults.end(); ++field)
        if (!scope.contains(field.key())) scope[field.key()]=field.value();
    captureGridDraft(false);if (!resolveDraft()) return;
    if (!matchesTarget(scope)) { issue("Reviewed list inputs changed. Review the scope again."); return; }
    scope.remove("workbook"); scope.remove("expected_state"); scope.remove("sheet");
    m_listSpec=scope; applyList();
}

void MainWindow::applyList() {
    if (m_listSpec.isEmpty()) return;
    const auto selected=m_grid->currentIndex();
    const int selectedRow=selected.isValid()?m_model->sourceRow(selected.row()):-1;
    const int selectedColumn=selected.isValid()?selected.column():0;
    const QString workbook=m_info["id"].toString(),sheet=m_model->sheet();const auto service=m_client->url();
    auto request = m_listSpec; request["sheet"] = m_model->sheet(); setBusy(true, "Evaluating full list scope…");
    m_client->request(base()+"/list", request, [this,selectedRow,selectedColumn,workbook,sheet,service](QJsonObject result, QString error) {
        if(m_info["id"].toString()!=workbook || m_model->sheet()!=sheet || m_client->url()!=service)return;
        if (!error.isEmpty() || result["state"] != m_info["state"]) {
            const bool stale=error.isEmpty();m_listSpec={};
            m_model->bind(m_info["id"].toString(),sheet,m_info["state"].toString(),m_info["readonly"].toBool());restoreViewState();
            m_grid->setCurrentIndex(m_model->index(qMax(0,selectedRow),selectedColumn));setBusy(false,"List not applied • Source sheet");
            issue(stale?"List inputs changed. Refreshing source data; review the scope again.":error);
            if(stale)refresh();else emit workbookReady();return;
        }
        const auto scope=result["scope"].toArray();
        if(scope.size()==4){m_listSpec["r1"]=scope[0];m_listSpec["c1"]=scope[1];m_listSpec["r2"]=scope[2];m_listSpec["c2"]=scope[3];}
        m_listSpec["header"]=m_listSpec["header"].toBool(false);
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

void MainWindow::traceFormula(const QString &direction) {
    if(m_busy || m_unknown || m_info.isEmpty() || m_model->projected())return;
    const auto current=QPersistentModelIndex(m_grid->currentIndex());if(!current.isValid())return;
    const auto scope=targetContext();const auto service=m_client->url();captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(scope) || m_grid->currentIndex()!=current)return;
    auto request=scope;request.remove("workbook");request["row"]=current.row()+1;request["col"]=current.column()+1;request["direction"]=direction;
    setBusy(true,"Tracing stored formula references…");
    m_client->request(base()+"/trace",request,[this,scope,service,current,direction](QJsonObject result,QString error){
        if(service!=m_client->url() || scope["workbook"]!=m_info["id"])return;
        setBusy(false);
        bool editor=false;for(auto *view:m_frozenTop->views())if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))editor|=delegate->hasEditor();
        if(auto *delegate=dynamic_cast<CellDelegate *>(m_grid->itemDelegate()))editor|=delegate->hasEditor();
        if(!matchesTarget(scope) || m_grid->currentIndex()!=current || m_model->projected() || m_source->isModified() || editor)return;
        if(!error.isEmpty()){issue(error);return;}
        if(result["state"]!=scope["expected_state"] || result["sheet"]!=scope["sheet"] || result["row"].toInt()!=current.row()+1 || result["col"].toInt()!=current.column()+1 || result["direction"].toString()!=direction || result["semantics"]!="direct_syntactic" || !result["targets"].isArray() || !result["issues"].isArray() || result["targets"].toArray().size()>1000 || result["issues"].toArray().size()>1000){issue("Invalid formula trace response. Refresh and try again.");return;}
        for(const auto &value:result["targets"].toArray())if(!value.isObject() || !validTraceTarget(value.toObject(),m_info["sheets"].toArray())){issue("Invalid formula trace target. Refresh and try again.");return;}
        QDialog dialog(this);dialog.setObjectName("formulaTraceDialog");dialog.setWindowTitle(direction=="precedents"?tr("Direct precedents"):tr("Direct dependents"));dialog.resize(620,520);auto *layout=new QVBoxLayout(&dialog);
        auto *description=new QLabel(tr("Direct stored references, including unexecuted branches. Go to selected shows the first cell of a range and reveals its row and column if hidden."),&dialog);description->setWordWrap(true);layout->addWidget(description);
        auto *details=new QPlainTextEdit(&dialog);details->setObjectName("formulaTraceDetails");details->setReadOnly(true);details->setMaximumHeight(130);QStringList lines;
        for(const auto &value:result["targets"].toArray()){const auto target=value.toObject();lines.append(target["sheet_name"].toString()+"!"+target["address"].toString());}
        if(lines.isEmpty())lines.append(tr("No direct references found."));
        for(const auto &value:result["issues"].toArray()){const auto problem=value.toObject();lines.append(tr("Broken reference at %1: %2").arg(problem["sheet_name"].toString()+"!"+problem["address"].toString(),problem["message"].toString()));}
        details->setPlainText(lines.join('\n'));layout->addWidget(details);
        auto *targets=new QListWidget(&dialog);targets->setObjectName("traceTargets");targets->setAccessibleName(tr("Navigable trace targets"));for(const auto &value:result["targets"].toArray()){const auto target=value.toObject();auto *item=new QListWidgetItem(target["sheet_name"].toString()+"!"+target["address"].toString(),targets);item->setData(Qt::UserRole,QVariant::fromValue(target));}layout->addWidget(targets,1);if(targets->count())targets->setCurrentRow(0);
        auto *buttons=new QDialogButtonBox(QDialogButtonBox::Close,&dialog);auto *go=buttons->addButton(tr("Go to selected"),QDialogButtonBox::ActionRole);go->setObjectName("traceGoButton");go->setEnabled(targets->count()>0);QJsonObject chosen;
        connect(go,&QPushButton::clicked,&dialog,[&]{if(auto *item=targets->currentItem()){chosen=item->data(Qt::UserRole).toJsonObject();dialog.accept();}});connect(targets,&QListWidget::itemDoubleClicked,&dialog,[go]{go->click();});connect(buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);layout->addWidget(buttons);
        if(dialog.exec()==QDialog::Accepted && !chosen.isEmpty())navigateTrace(chosen,scope);
    });
}

void MainWindow::navigateTrace(const QJsonObject &target,const QJsonObject &scope) {
    if(m_busy || m_unknown || !matchesTarget(scope) || m_model->projected() || m_source->isModified() || !validTraceTarget(target,m_info["sheets"].toArray()))return;
    const auto current=QPersistentModelIndex(m_grid->currentIndex());const auto generation=m_navigationGeneration,editorGeneration=m_referenceGeneration;const auto service=m_client->url();
    m_status->setText("Checking trace navigation…");
    m_client->request(base(),{},[this,target,scope,current,generation,editorGeneration,service](QJsonObject info,QString error){
        if(service!=m_client->url() || generation!=m_navigationGeneration || editorGeneration!=m_referenceGeneration)return;
        bool editor=false;auto views=m_frozenTop->views();views.append(m_grid);for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))editor|=delegate->hasEditor();
        if(m_busy || m_unknown || !matchesTarget(scope) || m_grid->currentIndex()!=current || m_model->projected() || m_source->isModified() || m_address->isModified() || editor)return;
        if(!error.isEmpty() || info["id"]!=scope["workbook"] || info["state"]!=scope["expected_state"] || !validTraceTarget(target,info["sheets"].toArray())){issue(error.isEmpty()?"Trace results expired. Refresh and trace again.":error);return;}
        int tab=-1;for(int i=0;i<m_tabs->count();++i)if(m_tabs->tabData(i).toString()==target["sheet"].toString())tab=i;if(tab<0){issue("Trace target sheet is unavailable. Refresh and trace again.");return;}
        m_tabs->setCurrentIndex(tab);if(m_model->sheet()!=target["sheet"].toString())return;
        const int row=target["r1"].toInt()-1,col=target["c1"].toInt()-1;const bool rowShown=m_hiddenRows.remove(row),columnShown=m_hiddenColumns.remove(col);if(rowShown || columnShown){applyVisibility();saveViewState();}
        const auto cell=m_model->index(row,col);m_grid->setCurrentIndex(cell);m_grid->selectionModel()->select(cell,QItemSelectionModel::ClearAndSelect);m_grid->scrollTo(cell);m_frozenTop->focusCurrent();m_status->setText(tr("Trace target %1!%2 • Showing the first cell").arg(target["sheet_name"].toString(),target["address"].toString()));
    },false);
}

void MainWindow::clearFindResults() {
    ++m_findGeneration;m_findLoading=false;m_findScope={};m_findMatches={};m_findIndex=-1;m_findTotal=0;m_findPageOffset=0;
    if(m_findNext)m_findNext->setEnabled(false);
    if(m_findPrevious)m_findPrevious->setEnabled(false);
}

bool MainWindow::validFindPage(const QJsonObject &result,const QJsonObject &scope,int offset) const {
    auto integer=[](QJsonValue value,int low,int high){return value.isDouble() && value.toDouble()==value.toInt(-1) && value.toInt()>=low && value.toInt()<=high;};
    if(result["state"]!=scope["expected_state"] || !integer(result["count"],0,10000) || !integer(result["offset"],offset,offset) || !integer(result["limit"],500,500) || !result["matches"].isArray())return false;
    const auto matches=result["matches"].toArray();if(matches.size()!=qMin(500,qMax(0,result["count"].toInt()-offset)))return false;
    int lastRow=0,lastCol=0;
    for(const auto &value:matches){const auto cell=value.toObject();if(!value.isObject() || !integer(cell["row"],scope["r1"].toInt(),scope["r2"].toInt()) || !integer(cell["col"],scope["c1"].toInt(),scope["c2"].toInt()))return false;
        const int row=cell["row"].toInt(),col=cell["col"].toInt();if(row<lastRow || (row==lastRow && col<=lastCol))return false;lastRow=row;lastCol=col;
    }
    return true;
}

void MainWindow::showFindMatch(int index) {
    const int local=index-m_findPageOffset;if(local<0 || local>=m_findMatches.size())return;
    const auto match=m_findMatches[local].toObject();const int row=match["row"].toInt(),col=match["col"].toInt();
    if(intersectsHidden(row,col,row,col)){issue("The next match is hidden. Show its row/column before navigating to it.");return;}
    const auto cell=m_model->index(row-1,col-1);m_grid->setCurrentIndex(cell);m_grid->selectionModel()->select(cell,QItemSelectionModel::ClearAndSelect);if(m_grid->currentIndex()!=cell)return;
    m_findIndex=index;m_frozenTop->focusCurrent();m_status->setText(tr("Find %1 of %2 matches • Wraparound within searched selection").arg(index+1).arg(m_findTotal));
}

void MainWindow::navigateFind(int direction) {
    if(m_busy || m_unknown || m_findLoading || m_findTotal<=0)return;
    if(m_model->projected() || !matchesTarget(m_findScope)){clearFindResults();issue("Find results expired. Search the selection again.");return;}
    const auto scope=m_findScope;captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || m_unknown || !matchesTarget(scope) || scope!=m_findScope){clearFindResults();return;}
    const int next=m_findIndex<0?(direction<0?m_findTotal-1:0):(m_findIndex+direction+m_findTotal)%m_findTotal;
    if(next>=m_findPageOffset && next<m_findPageOffset+m_findMatches.size()){showFindMatch(next);return;}
    const int offset=(next/500)*500;const auto generation=m_findGeneration;const auto service=m_client->url();const auto current=QPersistentModelIndex(m_grid->currentIndex());const auto selection=m_grid->selectionModel()->selection();
    m_findLoading=true;m_findNext->setEnabled(false);m_findPrevious->setEnabled(false);m_status->setText("Loading next Find page…");auto request=scope;request.remove("workbook");request["offset"]=offset;request["limit"]=500;
    m_client->request(base()+"/find",request,[this,scope,generation,service,current,selection,offset,next](QJsonObject result,QString error){
        if(generation!=m_findGeneration || service!=m_client->url())return;
        m_findLoading=false;
        if(m_busy || m_unknown || m_model->projected() || !matchesTarget(scope) || scope!=m_findScope){clearFindResults();return;}
        if(!error.isEmpty() || !validFindPage(result,scope,offset) || result["count"].toInt()!=m_findTotal){clearFindResults();issue(error.isEmpty()?"Find results changed or the page is invalid. Refresh and search again.":error);return;}
        m_findNext->setEnabled(true);m_findPrevious->setEnabled(true);
        bool editor=false;auto views=m_frozenTop->views();views.append(m_grid);for(auto *view:views)if(auto *delegate=dynamic_cast<CellDelegate *>(view->itemDelegate()))editor|=delegate->hasEditor();
        if(m_grid->currentIndex()!=current || m_grid->selectionModel()->selection()!=selection || m_source->isModified() || editor){m_status->setText("Find navigation cancelled • Selection or draft changed");return;}
        m_findMatches=result["matches"].toArray();m_findPageOffset=offset;showFindMatch(next);
    });
}

void MainWindow::findDialog(bool replace) {
    if(m_busy || m_unknown || m_info.isEmpty())return;
    auto scope = selectedRange(); if (scope.isEmpty()) return;
    scope=targetContext(scope);
    const auto workbook=m_info["id"].toString();const auto service=m_client->url();
    captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(scope)) {issue("Search target changed. Review the scope again.");return;}
    bool ok;
    auto needle = QInputDialog::getText(this, tr("Find in selected rectangle"), tr("Case-sensitive text"), QLineEdit::Normal, {}, &ok);
    if (!ok || needle.isEmpty()) return;
    auto field = QInputDialog::getItem(this, tr("Search field"), tr("Search"), {tr("Stored input / formula source"), tr("Displayed value")}, 0, false, &ok);
    if (!ok) return;
    if (m_busy || service!=m_client->url() || workbook!=m_info["id"].toString() || scope["expected_state"]!=m_info["state"]
        || scope["sheet"].toString()!=m_model->sheet()) { issue("Search inputs changed. Refresh and review the scope again."); return; }
    scope["needle"] = needle; scope["field"] = field == tr("Displayed value") ? "display" : "source";
    setBusy(true, "Finding within declared scope…");
    const auto generation=m_findGeneration;
    auto request=scope; request.remove("workbook");request["offset"]=0;request["limit"]=500;
    m_client->request(base()+"/find", request, [this, scope, replace, workbook,generation,service](QJsonObject result, QString error) mutable {
        if (generation!=m_findGeneration || service!=m_client->url() || m_info["id"].toString()!=workbook) return;
        setBusy(false);
        if (!error.isEmpty()) { issue(error); return; }
        if (result["state"] != scope["expected_state"] || result["state"]!=m_info["state"]
            || scope["sheet"].toString()!=m_model->sheet()) { issue("Search inputs changed. Refresh and review the scope again."); return; }
        if(!validFindPage(result,scope,0)){issue("Invalid Find page. Refresh and search again.");return;}
        QStringList locations; for (const auto &value : result["matches"].toArray()) locations.append(value.toObject()["address"].toString());
        issue(tr("%1 matching cells: %2%3").arg(result["count"].toInt()).arg(locations.join(", ")).arg(result["truncated"].toBool() ? " (first 500 shown)" : ""));
        if(!replace) {
            m_findScope=scope;m_findMatches=result["matches"].toArray();m_findTotal=result["count"].toInt();m_findIndex=-1;m_findPageOffset=0;
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
    if(m_allowJobClose){
        if(!m_workspace->resolveDrafts()){m_allowJobClose=false;event->ignore();return;}
        m_workspace->commitDiscard();m_workspace->close();event->accept();return;
    }
    if(m_queryClosing){event->ignore();return;}
    const bool skipQueryCleanup=m_skipQueryCleanup;m_skipQueryCleanup=false;
    if(!skipQueryCleanup && !m_released && m_relational->hasQueryJob()){
        event->ignore();m_queryClosing=true;m_relational->closeQuery([this](QString error){m_queryClosing=false;if(!error.isEmpty()){
            const auto draft=m_relational->queryDraft();const auto job=draft["query_job"].toString();if(!preserveJobRecovery(m_info,job,{{"kind","read-only query"},{"error",error},{"query_draft",draft}}))return;
            QMessageBox review(QMessageBox::Warning,tr("Close with unconfirmed query cleanup?"),tr("Query cancellation or private storage release was not confirmed. The query is read-only. Keep its job ID and recovery record before closing.\nWorkbook: %1\nQuery: %2\nRecovery: %3\n%4").arg(m_info["path"].toString(),job,m_jobRecoveryRecord,error),QMessageBox::Yes|QMessageBox::Cancel,this);review.setObjectName("unconfirmedQueryClose");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setDefaultButton(QMessageBox::Cancel);if(review.exec()!=QMessageBox::Yes)return;m_skipQueryCleanup=true;
        }close();});return;
    }
    if(m_jobClosing){event->ignore();return;}
    if(!m_activeOperationJob.isEmpty()){
        event->ignore();
        const auto answer=QMessageBox::warning(this,tr("Cancel job and close?"),tr("Request cancellation of job %1, inspect its outcome, then release this window's workbook handle? Committed batches remain stored.\nWorkbook: %2").arg(m_activeOperationJob,m_info["path"].toString()),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel);
        if(answer!=QMessageBox::Yes)return;
        const auto workbook=m_info;const auto job=m_activeOperationJob;const auto session=m_client->generation();
        m_jobClosing=true;m_releasing=true;setBusy(true,"Cancelling job and confirming handle release…");
        auto *closer=new JobCloser(m_client,base(),job,m_activeJobStatus,[this,workbook,job,session](QJsonObject status,QString error){
            m_jobClosing=false;m_releasing=false;
            if(session!=m_client->generation() || workbook["id"]!=m_info["id"])error="Service or workbook changed during close; inspect the captured job.";
            if(error.isEmpty()){
                m_released=true;m_activeOperationJob.clear();
                if(!m_jobRecoveryRecord.isEmpty()){QFile::remove(m_jobRecoveryRecord);m_jobRecoveryRecord.clear();}
                m_allowJobClose=true;close();return;
            }
            setBusy(false,"Job close unresolved • Inspect retained journal");issue(error);
            if(!preserveJobRecovery(workbook,job,status))return;
            QMessageBox review(QMessageBox::Warning,tr("Close with unresolved job cleanup?"),tr("Cancellation or handle release could not be confirmed. Closing does not prove rollback or stop further commits.\nJob: %1\nWorkbook: %2\nRecovery record: %3\n\nClose anyway and inspect the retained journal before resuming?").arg(job,workbook["path"].toString(),m_jobRecoveryRecord),QMessageBox::Yes|QMessageBox::Cancel,this);
            review.setObjectName("unresolvedJobCloseReview");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setTextFormat(Qt::PlainText);review.setDefaultButton(QMessageBox::Cancel);
            if(review.exec()==QMessageBox::Yes){m_allowJobClose=true;close();}
        },this);
        closer->start();return;
    }

    if (m_unknown && !m_pendingId.isEmpty()) {
        if (!preserveUnknownOperation()) { issue("Keep the workbook path and operation ID before exiting: "+m_pendingId); event->ignore(); return; }
        const auto answer=QMessageBox::warning(this,tr("Close with an unknown operation outcome?"),
            tr("The edit may have committed. Closing does not discard stored data.\n\nWorkbook: %1\nOperation: %2\nRecovery record: %3\n\nReopen and look up this ID before retrying the edit.")
                .arg(m_info["path"].toString(),m_pendingId,m_recoveryRecord),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel);
        if (answer==QMessageBox::Yes) {
            if(!m_workspace->resolveDrafts()){event->ignore();return;}
            m_workspace->commitDiscard(); m_workspace->close(); event->accept(); return;
        }
        event->ignore(); return;
    }
    if (!m_pendingId.isEmpty() || m_unknown || m_releasing) {
        issue("Wait for a known operation outcome before closing. Committed work remains in the native workbook."); event->ignore(); return;
    }
    if(!discardQueryDraft()){event->ignore();return;}
    captureGridDraft(false);
    if(!m_relational->retainedDraft().isEmpty()){
        if(QMessageBox::question(this,tr("Discard table draft?"),tr("Discard the retained table input before closing? Stored records remain in the workbook."))!=QMessageBox::Yes){event->ignore();return;}
        m_relational->restoreDraft({});
    }
    // A Python Discard takes effect only when the close is accepted below.
    if(!m_workspace->resolveDrafts()){event->ignore();return;}
    if (m_source->isModified() || !m_recoveredDraft.isEmpty()) {
        if (QMessageBox::question(this, tr("Discard editor draft?"), tr("Discard the unsubmitted formula-bar draft? Already stored edits remain in the workbook.")) != QMessageBox::Yes) { m_workspace->keepDrafts(); event->ignore(); return; }
        m_source->setModified(false); clearRecoveredDraft();
    }
    if (!m_options.url.isEmpty() && !m_info.isEmpty() && !m_released) {
        const auto workbook=m_info["id"].toString(), service=m_client->url();
        m_releasing=true; setBusy(true,"Closing workbook…"); event->ignore();
        m_client->request(base()+"/close",{},[this,workbook,service](QJsonObject,QString){
            if (!m_releasing) { m_workspace->keepDrafts(); return; }
            m_releasing=false;
            if (m_info["id"].toString()!=workbook || m_client->url()!=service) { m_workspace->keepDrafts(); setBusy(false,"Current workbook remains open"); return; }
            m_released=true; close();
        });
        return;
    }
    m_workspace->commitDiscard();
    m_workspace->close();
    event->accept();
}

bool MainWindow::discardQueryDraft(){
    if(!m_relational->hasUnsavedQuery())return true;
    QMessageBox review(QMessageBox::Question,tr("Discard SQL definition draft?"),tr("Discard the unsubmitted SQL and parameter definition? Saved native queries and stored data remain."),QMessageBox::Yes|QMessageBox::Cancel,this);review.setObjectName("discardQueryDraft");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setDefaultButton(QMessageBox::Cancel);
    if(review.exec()!=QMessageBox::Yes)return false;m_relational->discardQueryDraft();return true;
}

bool MainWindow::eventFilter(QObject *object, QEvent *event) {
    if(event->type()==QEvent::Resize || event->type()==QEvent::Show || event->type()==QEvent::Hide) {
        if(object==m_grid || (m_frozenTop && m_frozenTop->views().contains(qobject_cast<QTableView *>(object))))
            QTimer::singleShot(0,this,[this]{updateVisibleCalculations();});
    }
    if(m_painterPending || !m_painterRows.isEmpty()) {
        if(event->type()==QEvent::KeyPress){const auto key=static_cast<QKeyEvent *>(event)->key();cancelFormatPainter();if(key==Qt::Key_Escape)return true;}
        else if(!m_painterRows.isEmpty() && formatPainterMouseEvent(object,event))return true;
    }
    if(m_pickingReference){
        if(object==m_source && event->type()==QEvent::MouseButtonPress)finishReferencePick(false);
        if(event->type()==QEvent::KeyPress){const auto *key=static_cast<QKeyEvent *>(event);finishReferencePick(false);if(key->key()==Qt::Key_Escape)return true;}
        else if(referenceMouseEvent(object,event))return true;
    }
    if (object == m_source && event->type() == QEvent::KeyPress) {
        const auto *key=static_cast<QKeyEvent *>(event);
        ++m_referenceGeneration;
        if(auto *line=dynamic_cast<FormulaLineEdit *>(m_source))if(line->completionKey(key))return true;
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
    layout.addRow(tr("Sheet number"),&sheet); layout.addRow(tr("Inputs"),&mode);QCheckBox utc(tr("Normalize explicit timestamp offsets to UTC (original offset omitted)"),&dialog);utc.setObjectName("importTimestampUtc");layout.addRow(&utc);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,&dialog); layout.addRow(&buttons);
    connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept); connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted || !matchesTarget(target)) return;
    target["timezone_policy"]=utc.isChecked()?"utc":"reject";importSpreadsheetBytes(bytes,format,mode.currentData().toString(),sheet.value(),target);
}

void MainWindow::importSpreadsheetBytes(const QByteArray &bytes, const QString &format, const QString &mode, int sheetIndex, QJsonObject target) {
    if(m_busy || m_unknown || m_info.isEmpty() || m_info["readonly"].toBool() || m_model->projected()) return;
    if(target.isEmpty()) target=csvImportTarget();
    if(target.isEmpty()) return;
    captureGridDraft(false); if(!resolveDraft() || !matchesTarget(target)) return;
    setBusy(true,tr("Preparing %1 import…").arg(format.toUpper()));
    prepareImport({{"file_base64",QString::fromLatin1(bytes.toBase64())},{"mode",mode},{"sheet_index",sheetIndex},
                   {"scope",sheetIndex==0?"workbook":"selection"},{"timezone_policy",target["timezone_policy"].toString("reject")},
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
    if(m_model->projected()){
        const auto target=targetContext();const auto criteria=m_listSpec;const auto hiddenRows=m_hiddenRows,hiddenColumns=m_hiddenColumns;
        ExportFileDialog dialog(this,{tr("CSV (*.csv)"),tr("Excel workbook (*.xlsx)"),tr("OpenDocument spreadsheet (*.ods)")});dialog.setWindowTitle(tr("Export visible list values (formulas become values)"));
        if(dialog.exec()==QDialog::Accepted && matchesTarget(target) && m_model->projected() && m_listSpec==criteria && m_hiddenRows==hiddenRows && m_hiddenColumns==hiddenColumns)exportListTo(dialog.selectedFiles().value(0),dialog.format());return;
    }
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

void MainWindow::exportListTo(const QString &path,const QString &format) {
    if(m_busy || m_unknown || path.isEmpty() || !QStringList{"csv","xlsx","ods"}.contains(format))return;
    const auto target=listValuesRequest(false);if(target.isEmpty())return;
    const auto criteria=m_listSpec;const auto hiddenRows=m_hiddenRows,hiddenColumns=m_hiddenColumns;const auto service=m_client->url();const auto native=m_info["path"].toString();
    if(sameFile(path,native)){issue("Export destination is the native workbook. Choose another file.");return;}
    captureGridDraft(false);if(!resolveDraft())return;
    if(m_busy || !matchesTarget(target) || !m_model->projected() || m_listSpec!=criteria)return;
    auto request=target;request.remove("workbook");setBusy(true,"Preparing visible list values…");
    m_client->request(base()+"/list/export/"+format,request,[this,target,criteria,hiddenRows,hiddenColumns,service,native,path,format](QJsonObject result,QString error){
        if(service!=m_client->url() || !matchesTarget(target))return;
        if(!error.isEmpty() || !m_model->projected() || m_listSpec!=criteria || m_hiddenRows!=hiddenRows || m_hiddenColumns!=hiddenColumns || result["state"]!=target["expected_state"]){setBusy(false);issue(error.isEmpty()?"List export inputs changed; review again.":error);return;}
        QStringList report;for(const auto &line:result["report"].toArray())report.append(line.toString());
        QMessageBox review(QMessageBox::Question,tr("Review list values export"),tr("Save this visible list snapshot?"),QMessageBox::Yes|QMessageBox::Cancel,this);review.setObjectName("listExportReview");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setTextFormat(Qt::PlainText);review.setDefaultButton(QMessageBox::Cancel);review.setInformativeText(report.join('\n'));
        const auto choice=review.exec();setBusy(false);
        if(choice!=QMessageBox::Yes || service!=m_client->url() || !matchesTarget(target) || !m_model->projected() || m_listSpec!=criteria || m_hiddenRows!=hiddenRows || m_hiddenColumns!=hiddenColumns)return;
        if(sameFile(path,native)){issue("Export destination resolves to the native workbook. Choose another file.");return;}
        if(format=="csv"){QString failure;if(!saveCsvSnapshot(path,result,native,failure))issue(failure);else m_status->setText("List CSV and available date/time companion exported");return;}
        QString failure;
        if(!ExportWriter::save(path,native,result,failure))issue(failure);
        else m_status->setText("List values exported in display order • Native workbook retained");
    });
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
        QString failure;
        if(!ExportWriter::save(path,native,result,failure)) issue(failure);
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
