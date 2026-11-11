// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include "restclient.h"
#include "sheetmodel.h"
#include <QMainWindow>
#include "formulasyntax.h"
#include <QProcess>
#include <QMap>
#include <QJsonArray>
#include <QPersistentModelIndex>
#include <QItemSelection>
#include <functional>

class QTableView;
class QLineEdit;
class QTabBar;
class QLabel;
class QPlainTextEdit;
class QToolBar;
class QPushButton;
class FrozenPanes;
class QTimer;
class QMenu;
class QStackedWidget;
class RelationalWorkbench;
class PythonWorkspace;
class NamesPanel;
class NameManagerDialog;

// A destination outside the formula bar for references picked in the grid.
// insert receives the exact-match name, or the always-quoted qualified address
// ('Supplies'!B3, 'Supplies'!B2:B4). stillValid is asked before each insertion;
// false ends picking without inserting.
struct ReferencePickTarget { std::function<void(const QString &)> insert; std::function<bool()> stillValid; };

struct StartupOptions { QString url, token, handler, recoveryDirectory, initialWorkbook, viewStatePath; bool automaticWorkbook = true; };

class MainWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit MainWindow(const StartupOptions &options = {}, QWidget *parent = nullptr);
    ~MainWindow() override;
    void openBook(const QString &path, bool create = false, bool readonly = false,bool repair=false);
    void submit(QJsonObject operation);
    void importCsvText(const QString &text, QJsonObject target = {});
    void importCsvBytes(const QByteArray &bytes, const QString &encoding = "utf-8-sig", const QString &delimiter = ",", QJsonObject target = {}, QJsonObject temporalSchema = {});
    void importSpreadsheetBytes(const QByteArray &bytes, const QString &format = "xlsx", const QString &mode = "formulas", int sheetIndex = 1, QJsonObject target = {});
    void inspectNativeFile(const QString &path);
    void exportSpreadsheetTo(const QString &path, const QString &format = "xlsx", const QString &mode = "formulas", QJsonObject target = {});
    void exportCsvTo(const QString &path, QJsonObject target = {});
    void saveNamedListView(const QString &name,bool replace = false);
    void applyNamedListView(const QString &name);
    void removeNamedListView(const QString &name);
    void exportListTo(const QString &path,const QString &format = "csv");
    void copyListValues(bool selectionOnly = false);
    void showList(QJsonObject scope);
    static bool isRecoveryPath(QString path);
    SheetModel *model() const { return m_model; }
    // The workbook's names as last read for the current handle, sorted by
    // case-insensitive identifier. Empty until the first read lands.
    QJsonArray names() const { return m_names; }
    // Arms picking for a target outside this window. It stays armed across
    // picks until cancelled (Escape in either window, the caller, or a guard
    // such as a busy window, a model reset or a reconfigured client); a sheet
    // switch by the user keeps it. Returns false, with an issue, when picking
    // cannot start. Starting it cancels a formula pick, and vice versa.
    bool startExternalReferencePick(ReferencePickTarget target,QString *problem = nullptr);
    void cancelExternalReferencePick();
    // True when this window started its service; an attached window never
    // approves code.
    bool ownsService() const { return m_options.url.isEmpty(); }
    // Sends one {"approve": key} line over the owned service's private
    // standard input. False, with nothing written, when attached, when the
    // process is not running, or when the key is malformed or names another
    // workbook than the open one. The outcome is read back from /code.
    bool grantCodeApproval(const QString &key);
    // Requests cooperative cancellation of operation, which must be the one in
    // flight; any other ID is refused. It is sent directly, not through submit,
    // because the window is busy with that operation. While the service does
    // not yet know the operation, the request is repeated every 100 ms until
    // it is accepted, the operation finishes, or the handle changes. The final
    // outcome still arrives as that operation's receipt.
    void cancelPendingOperation(const QString &operation);
signals:
    // Emitted once when external picking ends; inserted is true when at least
    // one reference was inserted while it was armed.
    void externalPickFinished(bool inserted);
    void serviceReady();
    void workbookReady();
    void operationFinished(bool committed);
    void operationAdmitted(QString operationId);
    void operationReceipt(QJsonObject receipt);
    void namesChanged();
protected:
    void closeEvent(QCloseEvent *event) override;
    bool eventFilter(QObject *object, QEvent *event) override;
private:
    void showRelational(bool visible);
    void showPythonWorkspace();
    void showNameManager();
    void readNames(const QJsonObject &info);
    void clearNames();
    void publishNames();
    void updateNameControls();
    bool nameWritable(QString *reason) const;
    void nameMessage(const QString &text);
    void connectNamesPanel(NamesPanel *panel);
    QJsonObject nameById(const QString &id) const;
    QJsonObject nameByText(const QString &text) const;
    QString selectionName() const;
    QString nameForRange(int r1,int c1,int r2,int c2) const;
    QString qualifiedReference(int r1,int c1,int r2,int c2) const;
    QString addressText(const QModelIndex &index) const;
    void updateAddress();
    bool selectionReference(QString *reference,QString *problem) const;
    void createName(QWidget *parent);
    void renameName(QWidget *parent,const QString &id);
    void retargetName(QWidget *parent,const QString &id);
    void deleteName(QWidget *parent,const QString &id);
    void submitName(QWidget *parent,const QJsonObject &target,QJsonObject operation,const QString &success,const QString &failure);
    void goToName(const QJsonObject &name);
    void goToRange(int r1,int c1,int r2,int c2,int row,int col);
    NameManagerDialog *m_nameManager=nullptr;
    quint64 m_namesGeneration=0;
    bool m_namesReady=false,m_namesReadFailed=false;
    QString m_namesHandle,m_nameOperation,m_nameSuccess,m_nameFailure;
    RelationalWorkbench *m_relational=nullptr;
    PythonWorkspace *m_workspace=nullptr;
    QStackedWidget *m_objectStack=nullptr;
    QList<QMenu *> m_sheetMenus;
    QList<QAction *> m_sheetFileActions;
    bool m_relationalActive=false;
    bool m_refreshFailed=false;
    bool m_queryClosing=false;
    bool m_skipQueryCleanup=false;
    bool discardQueryDraft();
    QString m_relationalAfterWrite;
    QJsonObject m_tablePromotionDraft;
    void createTableFromSelection();
    quint64 m_calculationGeneration=0;
    QString m_activeOperationJob;
    bool m_jobClosing=false,m_allowJobClose=false;
    QString m_jobRecoveryRecord;
    QJsonObject m_activeJobStatus;
    bool preserveJobRecovery(const QJsonObject &workbook,const QString &job,const QJsonObject &status);
    void operationJobs();
    void updateFormulaReferences();
    QList<QColor> formulaReferenceColors(int row,int column) const;
    void startReferencePick();
    void pickOtherSheetReference();
    void finishReferencePick(bool insert);
    bool referenceMouseEvent(QObject *object,QEvent *event);
    void insertExternalReference(int r1,int c1,int r2,int c2);
    void loadFunctionCatalog();
    void showFunctionHelp();
    void traceFormula(const QString &direction);
    void navigateTrace(const QJsonObject &target,const QJsonObject &scope);
    void startService();
    void buildUi();
    void newBook();
    void refresh(const QString &selectSheet = {});
    void acceptInfo(const QJsonObject &info, const QString &selectSheet = {});
    void issue(const QString &message);
    void setBusy(bool busy, const QString &message = {});
    void reconcile();
    void saveCopy(bool firstSave = false);
    void save(bool copy);
    void copy(bool cut = false);
    void paste(bool valuesOnly = false, bool formatsOnly = false);
    void clearCells(const QString &mode);
    void startFormatPainter();
    void cancelFormatPainter();
    bool formatPainterMouseEvent(QObject *object,QEvent *event);
    void showFormatCells();
    void enterTemporal();
    void enterLocalized();
    void calculateSelection();
    void updateVisibleCalculations();
    void portableView(bool save);
    void declareRecords();
    void recordCommand();
    void adjustDecimals(int change);
    void format(const QJsonObject &style, QJsonObject target = {}, bool replaceStyle = false);
    void importFile();
    void exportFile();
    void importCsvOptions(const QByteArray &bytes, QJsonObject target, const QString &delimiter, QJsonObject temporalSchema = {});
    void previewCsv(QJsonObject request, QJsonObject target);
    void prepareImport(QJsonObject request, RestClient::Callback callback, const QString &format = "csv");
    void importSpreadsheetOptions(const QByteArray &bytes, const QString &format, QJsonObject target);
    bool csvTypesDialog(QJsonObject &request, const QJsonObject &preview);
    QJsonObject csvImportTarget() const;
    QJsonObject listValuesRequest(bool selectionOnly);
    void namedListDialog(int action);
    void listDialog();
    void applyList();
    void updateListSummary();
    void findDialog(bool replace);
    void queueSummary();
    void requestSummary();
    void clearFindResults();
    void navigateFind(int direction);
    void showFindMatch(int index);
    bool validFindPage(const QJsonObject &result,const QJsonObject &scope,int offset) const;
    void changeSheet(int index);
    void changeStructure(bool columns, bool remove);
    void reportReferenceWarnings(const QJsonObject &receipt);
    void syncSource();
    void fitVisibleWrappedRows();
    void resizeSections(bool columns);
    void fitColumns();
    void resetDimensions();
    QPair<int,int> selectedSections(bool columns);
    QString viewSettingsPath() const;
    void saveViewState();
    void restoreViewState();
    void hideSections(bool columns);
    void showHidden(bool rows,bool columns);
    void applyVisibility();
    void ensureVisibleCurrent();
    bool intersectsHidden(int r1,int c1,int r2,int c2) const;
    void updateFrozenRow();
    void freezeAtCurrent();
    void navigateData(const QString &direction,bool extend);
    bool resolveDraft();
    void submitDraft(bool advance = false, int direction = 1);
    QJsonObject enterDestination(const QModelIndex &index, int direction, bool horizontal = false) const;
    void applyEnterNavigation();
    void restartService();
    QString recoveryDirectory() const;
    bool preserveUnknownOperation();
    void clearRecoveryRecord();
    void restoreRecoveredDraft();
    void clearRecoveredDraft();
    void captureGridDraft(bool needsReview = true);
    QJsonObject selectedRange() const;
    QJsonObject targetContext(QJsonObject arguments = {}) const;
    bool matchesTarget(const QJsonObject &target) const;
    QString base() const;
    StartupOptions m_options;
    RestClient *m_client;
    SheetModel *m_model;
    QProcess m_service;
    QByteArray m_startup;
    QTableView *m_grid = nullptr;
    QLineEdit *m_address = nullptr, *m_source = nullptr;
    QTabBar *m_tabs = nullptr;
    QLabel *m_status = nullptr, *m_summary = nullptr, *m_listSummary = nullptr;
    QTimer *m_summaryTimer = nullptr;
    QJsonObject m_summaryKey;
    quint64 m_summaryGeneration = 0, m_summaryFlight = 0;
    bool m_summaryInFlight = false;
    QPlainTextEdit *m_issues = nullptr;
    QToolBar *m_toolbar = nullptr;
    QAction *m_painterAction=nullptr,*m_painterCancel=nullptr;
    QJsonArray m_painterRows;QJsonObject m_painterOrigin,m_painterDestination;
    quint64 m_painterGeneration=0,m_painterDragGeneration=0;
    bool m_painterPending=false,m_painterDragging=false;QPoint m_painterAnchor;
    QAction *m_undo = nullptr, *m_redo = nullptr;
    QJsonObject m_info,m_functionCatalog;
    QJsonArray m_names;
    QLabel *m_formulaCues=nullptr;
    QList<FormulaReferenceCue> m_formulaReferences;
    QAction *m_pickReferenceAction = nullptr;
    quint64 m_referenceGeneration=0;
    bool m_pickingReference=false,m_referenceDragging=false;
    QJsonObject m_referenceTarget;
    QPersistentModelIndex m_referenceCurrent;
    QString m_referenceText,m_referencePreview,m_referenceIssue;
    int m_referenceStart=0,m_referenceLength=0;
    QPoint m_referenceAnchor;
    // External (Python) picking state; it shares the drag state above.
    QAction *m_externalPickCancel = nullptr;
    ReferencePickTarget m_externalTarget;
    bool m_externalPicking=false,m_externalInserted=false,m_changingSheet=false;
    QString m_externalWorkbook,m_externalSheet,m_externalStatus;
    QPersistentModelIndex m_externalCurrent;
    QItemSelection m_externalSelection;
    QJsonObject m_listSpec,m_namedViews;
    QJsonObject m_findScope;
    QList<QAction *> m_navigationActions;
    quint64 m_navigationGeneration = 0, m_navigationFlight = 0;
    bool m_navigationPending = false, m_navigationExtending = false, m_applyingNavigation = false;
    QPersistentModelIndex m_navigationAnchor;
    QJsonArray m_findMatches;
    int m_findIndex = -1, m_findTotal = 0, m_findPageOffset = 0;
    quint64 m_findGeneration = 0;
    bool m_findLoading = false;
    QAction *m_findNext = nullptr, *m_findPrevious = nullptr;
    QJsonObject m_draftTarget;
    QJsonObject m_recoveredDraft;
    QJsonObject m_enterRequest, m_enterNavigation;
    QString m_draftSubmissionId;
    bool m_restoreDraft = false, m_draftNeedsReview = false;
    QString m_pendingId;
    QString m_restartPath, m_recoveryRecord;
    quint64 m_clipboardGeneration = 0;
    bool m_busy = true, m_unknown = false, m_refreshing = false;
    int m_saveMode = 0;
    bool m_releasing = false, m_released = false;
    bool m_shuttingDown = false;
    QSet<int> m_wrappedRows;
    QMap<int,int> m_rowSizes, m_columnSizes;
    QSet<int> m_sizedRows, m_sizedColumns;
    QString m_viewKey;
    bool m_applyingView = false;
    QSet<int> m_hiddenRows, m_hiddenColumns, m_hiddenViewRows, m_hiddenViewColumns;
    QPushButton *m_hiddenButton = nullptr;
    FrozenPanes *m_frozenTop = nullptr;
    QAction *m_freezeAction = nullptr, *m_freezeColumnAction = nullptr, *m_freezePanesAction = nullptr, *m_unfreezePanesAction = nullptr;
    int m_frozenRows = 0, m_frozenColumns = 0;
    bool m_frozenSizeIssue = false;
};
