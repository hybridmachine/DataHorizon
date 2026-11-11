// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "restclient.h"
#include <QWidget>
#include <QJsonArray>

class QTreeWidget;
class QTableWidget;
class QLabel;
class QPushButton;

class RelationalWorkbench : public QWidget {
    Q_OBJECT
public:
    explicit RelationalWorkbench(RestClient *client,QWidget *parent=nullptr);
    void bind(const QJsonObject &info);
    void setLocked(bool locked);
    void refreshCatalog();
    void selectTable(const QString &id){m_table=id;}
    static bool editDefinition(QWidget *parent,QJsonObject &definition,const QString &title,bool fixedWidth=false);
signals:
    void submitRequested(QJsonObject operation);
    void issue(QString message);
    void sheetsRequested();
    void busyRequested(bool busy);
private:
    void createTable();
    void dropTable();
    void editSchema();
    void showTable(const QString &id);
    void loadRecords(const QString &after = {});
    QString base()const;
    RestClient *m_client;
    QJsonObject m_info,m_catalog,m_draftDefinition;
    QJsonObject m_schemaDraft;
    QString m_schemaDraftTable,m_schemaDraftState;
    QString m_table;
    quint64 m_generation=0;
    bool m_locked=true,m_blocked=true;
    quint64 m_readGeneration=0;
    QString m_nextAfter,m_after;
    QStringList m_previousPages;
    QJsonObject m_page;
    QTreeWidget *m_objects;
    QTableWidget *m_schema;
    QTableWidget *m_records;
    QPushButton *m_previous,*m_next;
    QLabel *m_status;
    QPushButton *m_create,*m_drop,*m_editSchema;
};
