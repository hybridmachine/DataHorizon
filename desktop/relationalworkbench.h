// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QWidget>
#include <QJsonArray>
#include <QPointer>
#include <functional>

class QTreeWidget;
class QTableWidget;
class QLabel;
class QPushButton;
class RecordInputDialog;
class QueryWorkbench;
class QTabWidget;

class RelationalWorkbench : public QWidget {
    Q_OBJECT
public:
    explicit RelationalWorkbench(RestClient *client,QWidget *parent=nullptr);
    void bind(const QJsonObject &info);
    void setLocked(bool locked);
    void refreshCatalog();
    void selectTable(const QString &id){if(id!=m_table){m_browse={};m_projection={};}m_table=id;}
    static bool editDefinition(QWidget *parent,QJsonObject &definition,const QString &title,bool fixedWidth=false);
    bool eventFilter(QObject *object,QEvent *event)override;
    void operationAdmitted(const QString &operation);
    void operationReceipt(const QJsonObject &receipt);
    void admissionRejected();
    bool hasActiveEditor()const;
    QJsonObject retainedDraft()const{return m_recordDraft;}
    void restoreDraft(const QJsonObject &draft);
    void suspend();
    QJsonObject cacheMetrics()const;
    bool hasQueryJob()const;
    void closeQuery(std::function<void(QString)> completion);
    QJsonObject queryDraft()const;
    bool hasUnsavedQuery()const;
    void discardQueryDraft();
    void exportTableTo(const QString &path,const QString &format);
signals:
    void submitRequested(QJsonObject operation);
    void issue(QString message);
    void sheetsRequested();
    void busyRequested(bool busy);
    void reconcileRequested();
    void cancelOperationRequested();
    void refreshRequested();
    void sheetSelected(QString id);
private:
    void createTable();
    void dropTable();
    void editSchema();
    void browseOptions();
    void chooseColumns();
    void manageIndexes();
    void manageRelationships();
    void inspectViolations();
    void editCell(int row,int column);
    void addRecord();
    void recordDialog(const QJsonObject &table,const QJsonArray &columns,const QJsonObject &stored,const QString &rowId,const QJsonObject &retained={});
    void reviewRecordDraft();
    void deleteRecords();
    void clearRecordCells();
    void copyRecordCells();
    void pasteRecordCells();
    void dataPreview(const QJsonObject &request,const QJsonObject &reviewed,QPointer<RecordInputDialog> dialog);
    void showTable(const QString &id);
    void loadRecords(const QString &after = {});
    QString base()const;
    RestClient *m_client;
    QueryWorkbench *m_query;
    QTabWidget *m_workbenchTabs;
    QJsonObject m_info,m_catalog,m_draftDefinition;
    QJsonObject m_schemaDraft;
    QJsonObject m_recordDraft;
    QPointer<RecordInputDialog> m_recordDialog;
    QString m_dataOperation;
    bool m_awaitingDataAdmission=false;
    QPushButton *m_addRecord,*m_editCell,*m_deleteRecords,*m_clearRecords,*m_copyRecords,*m_pasteRecords,*m_undoRecords,*m_redoRecords,*m_reviewDraft,*m_discardDraft,*m_inspectRecord;
    QLabel *m_draftStatus;
    QString m_schemaDraftTable,m_schemaDraftState;
    QString m_table;
    quint64 m_generation=0;
    bool m_locked=true,m_blocked=true;
    quint64 m_readGeneration=0;
    QString m_nextAfter,m_after;
    QStringList m_previousPages;
    QJsonObject m_page;
    QJsonObject m_browse;
    QJsonArray m_projection;
    QTreeWidget *m_objects;
    QTableWidget *m_schema;
    QTableWidget *m_records;
    QPushButton *m_previous,*m_next;
    QLabel *m_status;
    QPushButton *m_create,*m_drop,*m_editSchema;
};
