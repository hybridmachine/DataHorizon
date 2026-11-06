// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include "restclient.h"
#include "sheetmodel.h"
#include <QMainWindow>
#include <QProcess>
#include <QMap>

class QTableView;
class QLineEdit;
class QTabBar;
class QLabel;
class QPlainTextEdit;
class QToolBar;
class QPushButton;
class FrozenTopRow;

struct StartupOptions { QString url, token, handler, recoveryDirectory, initialWorkbook, viewStatePath; bool automaticWorkbook = true; };

class MainWindow : public QMainWindow {
    Q_OBJECT
public:
    explicit MainWindow(const StartupOptions &options = {}, QWidget *parent = nullptr);
    ~MainWindow() override;
    void openBook(const QString &path, bool create = false, bool readonly = false);
    void submit(QJsonObject operation);
    void importCsvText(const QString &text, QJsonObject target = {});
    void importCsvBytes(const QByteArray &bytes, const QString &encoding = "utf-8-sig", const QString &delimiter = ",", QJsonObject target = {});
    void exportCsvTo(const QString &path, QJsonObject target = {});
    void showList(QJsonObject scope);
    static bool isRecoveryPath(QString path);
    SheetModel *model() const { return m_model; }
signals:
    void serviceReady();
    void workbookReady();
    void operationFinished(bool committed);
protected:
    void closeEvent(QCloseEvent *event) override;
    bool eventFilter(QObject *object, QEvent *event) override;
private:
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
    void format(const QJsonObject &style, QJsonObject target = {});
    void importCsv();
    void previewCsv(QJsonObject request, QJsonObject target);
    bool csvTypesDialog(QJsonObject &request, const QJsonObject &preview);
    QJsonObject csvImportTarget() const;
    void exportCsv();
    void listDialog();
    void applyList();
    void findDialog(bool replace);
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
    bool resolveDraft();
    void submitDraft(bool advance = false, int direction = 1);
    QJsonObject enterDestination(const QModelIndex &index, int direction) const;
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
    QLabel *m_status = nullptr;
    QPlainTextEdit *m_issues = nullptr;
    QToolBar *m_toolbar = nullptr;
    QAction *m_undo = nullptr, *m_redo = nullptr;
    QJsonObject m_info;
    QJsonObject m_listSpec;
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
    FrozenTopRow *m_frozenTop = nullptr;
    QAction *m_freezeAction = nullptr;
    bool m_freezeTop = false;
};
