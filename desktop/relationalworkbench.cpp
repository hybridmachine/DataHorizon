// SPDX-License-Identifier: GPL-3.0-only
#include "relationalworkbench.h"
#include "recordinputdialog.h"
#include "queryworkbench.h"
#include <QApplication>
#include <QClipboard>
#include <QMimeData>
#include <QJsonDocument>
#include <QTimer>
#include <QGridLayout>
#include "listcriteriadialog.h"
#include <QListWidget>
#include <QCheckBox>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QInputDialog>
#include <QSet>
#include <QMessageBox>
#include <QPushButton>
#include <QSplitter>
#include <QTableWidget>
#include <QTabWidget>
#include <QTreeWidget>
#include <QVBoxLayout>

RelationalWorkbench::RelationalWorkbench(RestClient *client,QWidget *parent):QWidget(parent),m_client(client) {
    setObjectName("relationalWorkbench");
    auto *root=new QVBoxLayout(this);m_workbenchTabs=new QTabWidget;m_workbenchTabs->setObjectName("relationalPages");root->addWidget(m_workbenchTabs);
    auto *tablePage=new QWidget;auto *layout=new QVBoxLayout(tablePage);m_workbenchTabs->addTab(tablePage,tr("Tables"));m_query=new QueryWorkbench(client);m_workbenchTabs->addTab(m_query,tr("Read-only SQL"));connect(m_query,&QueryWorkbench::issue,this,&RelationalWorkbench::issue);
    auto *buttons=new QGridLayout;
    auto *sheets=new QPushButton(tr("Return to sheets"));sheets->setObjectName("returnToSheets");
    m_create=new QPushButton(tr("Create empty table…"));m_create->setObjectName("createTable");
    m_drop=new QPushButton(tr("Drop table…"));m_drop->setObjectName("dropTable");
    m_editSchema=new QPushButton(tr("Edit table columns…"));m_editSchema->setObjectName("editTableSchema");
    auto *refresh=new QPushButton(tr("Refresh tables"));refresh->setObjectName("refreshTables");
    int b=0;for(auto *button:{sheets,m_create,m_editSchema,m_drop,refresh}){buttons->addWidget(button,b/3,b%3);++b;}
    auto *browse=new QPushButton(tr("Sort and filter…"));browse->setObjectName("tableBrowseOptions");buttons->addWidget(browse,1,2);connect(browse,&QPushButton::clicked,this,&RelationalWorkbench::browseOptions);
    auto *columns=new QPushButton(tr("Choose displayed columns…"));columns->setObjectName("tableChooseColumns");buttons->addWidget(columns,2,0);connect(columns,&QPushButton::clicked,this,&RelationalWorkbench::chooseColumns);
    auto *indexes=new QPushButton(tr("Indexes…"));indexes->setObjectName("tableIndexes");buttons->addWidget(indexes,2,1);connect(indexes,&QPushButton::clicked,this,&RelationalWorkbench::manageIndexes);
    auto *cancel=new QPushButton(tr("Cancel current operation"));cancel->setObjectName("cancelTableOperation");buttons->addWidget(cancel,2,2);connect(cancel,&QPushButton::clicked,this,&RelationalWorkbench::cancelOperationRequested);
    auto *relations=new QPushButton(tr("Relationships…"));relations->setObjectName("tableRelationships");buttons->addWidget(relations,3,0);connect(relations,&QPushButton::clicked,this,&RelationalWorkbench::manageRelationships);
    auto *violations=new QPushButton(tr("Inspect relationship violations…"));violations->setObjectName("relationshipViolations");buttons->addWidget(violations,3,1);connect(violations,&QPushButton::clicked,this,&RelationalWorkbench::inspectViolations);
    layout->addLayout(buttons);
    auto *records=new QGridLayout;b=0;
    const QList<QPair<QPushButton **,QStringList>> actions{
        {&m_addRecord,{tr("Add record…"),"addTableRecord"}},{&m_editCell,{tr("Edit cell…"),"editTableCell"}},
        {&m_deleteRecords,{tr("Delete records…"),"deleteTableRecords"}},{&m_clearRecords,{tr("Clear cells…"),"clearTableCells"}},
        {&m_copyRecords,{tr("Copy values"),"copyTableValues"}},{&m_pasteRecords,{tr("Paste values…"),"pasteTableValues"}},
        {&m_undoRecords,{tr("Undo"),"undoTableRecords"}},{&m_redoRecords,{tr("Redo"),"redoTableRecords"}}};
    for(const auto &entry:actions){auto *button=new QPushButton(entry.second[0]);button->setObjectName(entry.second[1]);*entry.first=button;records->addWidget(button,b/4,b%4);++b;}layout->addLayout(records);
    auto *drafts=new QHBoxLayout;m_draftStatus=new QLabel;m_draftStatus->setWordWrap(true);drafts->addWidget(m_draftStatus,1);
    m_reviewDraft=new QPushButton(tr("Review retained draft"));m_discardDraft=new QPushButton(tr("Discard draft"));m_inspectRecord=new QPushButton(tr("Inspect operation"));
    m_reviewDraft->setObjectName("reviewRetainedTableDraft");m_discardDraft->setObjectName("discardTableDraft");
    for(auto *button:{m_reviewDraft,m_discardDraft,m_inspectRecord})drafts->addWidget(button);layout->addLayout(drafts);
    auto *copyDraft=new QPushButton(tr("Copy retained draft"));copyDraft->setObjectName("copyTableDraft");drafts->addWidget(copyDraft);connect(copyDraft,&QPushButton::clicked,this,[this]{if(!m_recordDraft.isEmpty())QApplication::clipboard()->setText(QString::fromUtf8(QJsonDocument(m_recordDraft).toJson()));});
    auto *splitter=new QSplitter;layout->addWidget(splitter,1);
    m_objects=new QTreeWidget; m_objects->setObjectName("relationalObjects");m_objects->setHeaderLabel(tr("Workbook objects"));
    m_objects->setAccessibleName(tr("Workbook tables and queries"));splitter->addWidget(m_objects);
    m_schema=new QTableWidget(0,4);m_schema->setObjectName("tableSchema");m_schema->setAccessibleName(tr("Table columns"));
    m_schema->setHorizontalHeaderLabels({tr("Column"),tr("Type"),tr("Required"),tr("Unique")});
    m_schema->setEditTriggers(QAbstractItemView::NoEditTriggers);m_schema->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
    auto *tabs=new QTabWidget;tabs->setObjectName("tableViews");
    m_records=new QTableWidget; m_records->setObjectName("tableRecords");m_records->setAccessibleName(tr("Table records"));
    m_records->setEditTriggers(QAbstractItemView::NoEditTriggers);m_records->setSelectionBehavior(QAbstractItemView::SelectItems);
    tabs->addTab(m_records,tr("Records"));tabs->addTab(m_schema,tr("Columns"));splitter->addWidget(tabs);splitter->setStretchFactor(1,1);
    auto *paging=new QHBoxLayout;m_previous=new QPushButton(tr("Previous page"));m_previous->setObjectName("previousTablePage");
    m_next=new QPushButton(tr("Next page"));m_next->setObjectName("nextTablePage");paging->addWidget(m_previous);paging->addWidget(m_next);paging->addStretch();layout->addLayout(paging);
    connect(m_next,&QPushButton::clicked,this,[this]{if(!m_nextAfter.isEmpty()){m_previousPages.append(m_after);loadRecords(m_nextAfter);}});
    connect(m_previous,&QPushButton::clicked,this,[this]{if(!m_previousPages.isEmpty())loadRecords(m_previousPages.takeLast());});
    m_status=new QLabel(tr("Choose a table or create an empty table."));m_status->setObjectName("tableStatus");m_status->setWordWrap(true);
    m_status->setAccessibleName(tr("Table operation status"));layout->addWidget(m_status);
    connect(sheets,&QPushButton::clicked,this,[this]{if(m_info["repair"].toBool())emit issue(tr("This handle is restricted to relationship repair. Reopen normally to edit sheets."));else emit sheetsRequested();});
    connect(refresh,&QPushButton::clicked,this,&RelationalWorkbench::refreshCatalog);
    connect(m_create,&QPushButton::clicked,this,&RelationalWorkbench::createTable);
    connect(m_drop,&QPushButton::clicked,this,&RelationalWorkbench::dropTable);
    connect(m_editSchema,&QPushButton::clicked,this,&RelationalWorkbench::editSchema);
    connect(m_addRecord,&QPushButton::clicked,this,&RelationalWorkbench::addRecord);
    connect(m_editCell,&QPushButton::clicked,this,[this]{editCell(m_records->currentRow(),m_records->currentColumn());});
    connect(m_records,&QTableWidget::cellDoubleClicked,this,&RelationalWorkbench::editCell);
    connect(m_deleteRecords,&QPushButton::clicked,this,&RelationalWorkbench::deleteRecords);
    connect(m_clearRecords,&QPushButton::clicked,this,&RelationalWorkbench::clearRecordCells);
    connect(m_copyRecords,&QPushButton::clicked,this,&RelationalWorkbench::copyRecordCells);
    connect(m_pasteRecords,&QPushButton::clicked,this,&RelationalWorkbench::pasteRecordCells);
    connect(m_undoRecords,&QPushButton::clicked,this,[this]{emit submitRequested({{"action","undo"}});});
    connect(m_redoRecords,&QPushButton::clicked,this,[this]{emit submitRequested({{"action","redo"}});});
    connect(m_reviewDraft,&QPushButton::clicked,this,&RelationalWorkbench::reviewRecordDraft);
    connect(m_discardDraft,&QPushButton::clicked,this,[this]{m_recordDraft={};setLocked(m_blocked);});
    connect(m_inspectRecord,&QPushButton::clicked,this,&RelationalWorkbench::reconcileRequested);
    connect(m_objects,&QTreeWidget::currentItemChanged,this,[this](QTreeWidgetItem *item){if(item)showTable(item->data(0,Qt::UserRole).toString());});
    connect(m_client,&RestClient::configured,this,[this]{++m_generation;m_info={};m_catalog={};m_objects->clear();m_schema->setRowCount(0);setLocked(true);});
    connect(m_workbenchTabs,&QTabWidget::currentChanged,this,[this](int index){if(index==1){++m_readGeneration;m_page={};m_records->setRowCount(0);m_records->setColumnCount(0);m_query->resumeView();}else{m_query->suspendView();if(!m_table.isEmpty())loadRecords();}});
    setLocked(true);
}

QString RelationalWorkbench::base()const{return "/v1/workbooks/"+m_info["id"].toString();}

void RelationalWorkbench::bind(const QJsonObject &info) {
    if(m_info["id"]!=info["id"]) {m_table.clear();m_catalog={};m_draftDefinition={};m_browse={};m_projection={};}
    ++m_generation;++m_readGeneration;m_info=info;m_query->bind(info);setLocked(info.isEmpty());
    if(isVisible() && !info.isEmpty())refreshCatalog();
}

void RelationalWorkbench::setLocked(bool locked) {
    m_blocked=locked || m_info.isEmpty();m_locked=m_blocked || m_info.value("readonly").toBool() || m_info.value("repair").toBool();
    m_query->setLocked(m_blocked);
    m_create->setEnabled(!m_locked);m_drop->setEnabled(!m_locked && !m_table.isEmpty());
    m_editSchema->setEnabled(!m_locked && !m_table.isEmpty());
    m_previous->setEnabled(!m_blocked && !m_previousPages.isEmpty());m_next->setEnabled(!m_blocked && !m_nextAfter.isEmpty());
    for(auto *button:{m_addRecord,m_editCell,m_deleteRecords,m_clearRecords,m_pasteRecords})button->setEnabled(!m_locked && !m_table.isEmpty() && m_recordDraft.isEmpty());
    m_copyRecords->setEnabled(!m_blocked && !m_table.isEmpty());
    m_undoRecords->setText(tr("Undo %1").arg(m_info["undo"].toString()));m_redoRecords->setText(tr("Redo %1").arg(m_info["redo"].toString()));
    m_undoRecords->setEnabled(!m_locked && m_recordDraft.isEmpty() && m_info["undo"].isString());m_redoRecords->setEnabled(!m_locked && m_recordDraft.isEmpty() && m_info["redo"].isString());
    const bool draft=!m_recordDraft.isEmpty();m_draftStatus->setText(draft?tr("An unsubmitted or unresolved table draft is retained. Review its original targets before retrying."):QString{});
    m_reviewDraft->setVisible(draft);m_reviewDraft->setEnabled(!m_locked && m_dataOperation.isEmpty());m_discardDraft->setVisible(draft);m_discardDraft->setEnabled(m_dataOperation.isEmpty());m_inspectRecord->setVisible(!m_dataOperation.isEmpty());
}

void RelationalWorkbench::refreshCatalog() {
    if(m_info.isEmpty())return;
    const auto generation=++m_generation;const auto state=m_info["state"];
    m_status->setText(tr("Loading table catalog…"));
    m_client->request(base()+"/tables",{{"expected_state",state}},[this,generation,state](QJsonObject result,QString error){
        if(generation!=m_generation)return;
        if(!error.isEmpty()){m_status->setText(error);emit issue(error);return;}
        if(result["state"]!=state){m_status->setText(tr("Catalog changed. Refresh the workbook."));return;}
        m_catalog=result;m_query->setCatalog(result);m_objects->blockSignals(true);m_objects->clear();
        auto *tables=new QTreeWidgetItem(m_objects,{tr("Tables")});QTreeWidgetItem *selected=nullptr;
        for(const auto &entry:result["tables"].toArray()) {
            const auto table=entry.toObject();auto *item=new QTreeWidgetItem(tables,{table["name"].toString()});item->setData(0,Qt::UserRole,table["id"]);
            if(table["id"].toString()==m_table)selected=item;
        }
        tables->setExpanded(true);m_objects->setCurrentItem(selected);m_objects->blockSignals(false);
        showTable(selected?selected->data(0,Qt::UserRole).toString():QString{});
        m_status->setText(result["tables"].toArray().isEmpty()?tr("No tables. Create an empty table to begin."):tr("Choose a table to inspect its columns."));
    });
}

void RelationalWorkbench::showTable(const QString &id) {
    if(m_table!=id){m_browse={};m_projection={};}
    m_table=id;m_schema->setRowCount(0);m_previousPages.clear();m_after.clear();m_nextAfter.clear();++m_readGeneration;
    m_records->setRowCount(0);m_records->setColumnCount(0);
    for(const auto &entry:m_catalog["tables"].toArray()) {
        const auto table=entry.toObject();if(table["id"].toString()!=id)continue;
        for(const auto &entry:table["columns"].toArray()) {
            const auto col=entry.toObject();const int row=m_schema->rowCount();m_schema->insertRow(row);
            const QStringList values{col["name"].toString(),col["type"].toString(),col["required"].toBool()?tr("Yes"):tr("No"),col["unique"].toBool()?tr("Yes"):tr("No")};
            for(int c=0;c<values.size();++c)m_schema->setItem(row,c,new QTableWidgetItem(values[c]));
        }
    }
    setLocked(m_blocked);
    if(!id.isEmpty() && m_workbenchTabs->currentIndex()==0)loadRecords();
}

void RelationalWorkbench::loadRecords(const QString &after) {
    if(m_table.isEmpty() || m_info.isEmpty())return;
    const auto generation=++m_readGeneration;const auto table=m_table;const auto state=m_info["state"];
    auto request=m_browse;request["table"]=table;request["expected_state"]=state;request["preview"]=true;request["count"]=true;
    if(!m_projection.isEmpty())request["columns"]=m_projection;
    request["limit"]=qMin(128,2048/qMax(1,m_projection.isEmpty()?16:int(m_projection.size())));if(!after.isEmpty())request["cursor"]=after;
    m_status->setText(tr("Loading records…"));m_client->request(base()+"/tables/read",request,[this,generation,table,state,after](QJsonObject result,QString error){
        if(generation!=m_readGeneration || m_table!=table || state!=m_info["state"])return;
        if(!error.isEmpty()){m_status->setText(error);emit issue(error);return;}
        if(result["state"]!=state || result["table"]!=table){m_status->setText(tr("Table page changed; refresh the workbook."));return;}
        const auto charge=3*(QJsonDocument(m_catalog).toJson(QJsonDocument::Compact).size()+QJsonDocument(result).toJson(QJsonDocument::Compact).size())+512*result["columns"].toArray().size()*result["records"].toArray().size()+256*m_schema->rowCount()+m_query->cacheMetrics()["estimated_bytes"].toInteger();
        if(charge>16*1024*1024){m_page={};m_records->setRowCount(0);m_status->setText(tr("This page exceeds the desktop cache budget. Choose fewer displayed columns."));return;}
        m_page=result;m_after=after;m_nextAfter=result["next_cursor"].toString();
        const auto cols=result["columns"].toArray(),rows=result["records"].toArray();m_records->clear();m_records->setRowCount(rows.size());m_records->setColumnCount(cols.size());QStringList labels;
        for(const auto &col:cols)labels.append(col.toObject()["name"].toString());m_records->setHorizontalHeaderLabels(labels);
        for(int r=0;r<rows.size();++r){const auto record=rows[r].toObject();for(int c=0;c<cols.size();++c){const auto value=record["values"].toObject()[cols[c].toObject()["id"].toString()];
            const bool omitted=record["omitted_columns"].toArray().contains(cols[c].toObject()["id"]);
            auto *item=new QTableWidgetItem(tableValueText(value)+(omitted?QStringLiteral("…"):QString{}));item->setData(Qt::UserRole,record["row_id"]);item->setToolTip(omitted?tr("Text preview; Edit and Copy retrieve the full stored value."):tr("Record %1").arg(record["row_id"].toString()));m_records->setItem(r,c,item);}}
        m_status->setText(tr("%1 records on this page. Matching count: %2. NULL sorts first ascending and last descending; record identity breaks ties.").arg(rows.size()).arg(result["count_status"]=="exact"?QString::number(result["count"].toInteger()):tr("unavailable")));setLocked(m_blocked);
    });
}

void RelationalWorkbench::suspend(){++m_generation;++m_readGeneration;m_catalog={};m_query->setCatalog({});m_query->suspendView();m_page={};m_objects->clear();m_schema->setRowCount(0);m_records->setRowCount(0);m_records->setColumnCount(0);m_previousPages.clear();}
bool RelationalWorkbench::hasQueryJob()const{return m_query->hasActiveJob();}
void RelationalWorkbench::closeQuery(std::function<void(QString)> completion){m_query->closeJob(std::move(completion));}
QJsonObject RelationalWorkbench::cacheMetrics()const{
    const auto bytes=QJsonDocument(m_catalog).toJson(QJsonDocument::Compact).size()+QJsonDocument(m_page).toJson(QJsonDocument::Compact).size();
    return {{"estimated_bytes",3*bytes+512*m_records->rowCount()*m_records->columnCount()+256*m_schema->rowCount()+m_query->cacheMetrics()["estimated_bytes"].toInteger()},{"budget_bytes",16*1024*1024},{"loaded_records",m_records->rowCount()},{"loaded_values",m_records->rowCount()*m_records->columnCount()+m_query->cacheMetrics()["loaded_values"].toInt()}};
}

void RelationalWorkbench::chooseColumns(){
    if(m_blocked || m_table.isEmpty())return;QJsonArray columns;for(const auto &entry:m_catalog["tables"].toArray())if(entry.toObject()["id"].toString()==m_table)columns=entry.toObject()["columns"].toArray();
    const auto state=m_info["state"];QDialog dialog(this);dialog.setWindowTitle(tr("Displayed table columns"));QVBoxLayout layout(&dialog);QListWidget list;list.setObjectName("tableColumnPicker");
    for(int i=0;i<columns.size();++i){const auto col=columns[i].toObject();auto *item=new QListWidgetItem(col["name"].toString(),&list);item->setData(Qt::UserRole,col["id"]);item->setFlags(item->flags()|Qt::ItemIsUserCheckable);item->setCheckState((m_projection.isEmpty()?i<16:m_projection.contains(col["id"]))?Qt::Checked:Qt::Unchecked);}layout.addWidget(&list);
    QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel);layout.addWidget(&buttons);connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);if(dialog.exec()!=QDialog::Accepted || state!=m_info["state"])return;
    QJsonArray selected;for(int i=0;i<list.count();++i)if(list.item(i)->checkState()==Qt::Checked)selected.append(list.item(i)->data(Qt::UserRole).toJsonValue());if(selected.isEmpty()){emit issue(tr("Choose at least one column."));return;}m_projection=selected;m_previousPages.clear();loadRecords();
}

void RelationalWorkbench::browseOptions(){
    if(m_blocked || m_table.isEmpty())return;QJsonArray columns;for(const auto &entry:m_catalog["tables"].toArray())if(entry.toObject()["id"].toString()==m_table)columns=entry.toObject()["columns"].toArray();if(columns.isEmpty())return;
    auto ordinal=[columns](const QJsonValue &id){for(int i=0;i<columns.size();++i)if(columns[i].toObject()["id"]==id)return i+1;return 0;};
    QJsonArray sort,filters;for(const auto &entry:m_browse["sort"].toArray()){const auto e=entry.toObject();sort.append(QJsonObject{{"col",ordinal(e["column"])},{"descending",e["direction"]=="desc"}});}
    const QMap<QString,QString> ops{{"eq","="},{"ne","<>"},{"lt","<"},{"le","<="},{"gt",">"},{"ge",">="},{"contains","contains"}};
    for(const auto &entry:m_browse["filters"].toArray()){const auto e=entry.toObject();filters.append(QJsonObject{{"col",ordinal(e["column"])},{"op",ops.value(e["op"].toString(),"=")},{"value",e["value"]}});}
    const auto state=m_info["state"];ListCriteriaDialog dialog({{"r1",1},{"r2",1},{"c1",1},{"c2",columns.size()},{"sort",sort},{"filters",filters},{"table_columns",columns}},this);
    if(dialog.exec()!=QDialog::Accepted || state!=m_info["state"])return;sort={};filters={};const auto scope=dialog.scope();
    for(const auto &entry:scope["sort"].toArray()){const auto e=entry.toObject();sort.append(QJsonObject{{"column",columns[e["col"].toInt()-1].toObject()["id"]},{"direction",e["descending"].toBool()?"desc":"asc"}});}
    for(const auto &entry:scope["filters"].toArray()){const auto e=entry.toObject();filters.append(QJsonObject{{"column",columns[e["col"].toInt()-1].toObject()["id"]},{"op",ops.key(e["op"].toString())},{"value",e["value"]}});}
    m_browse={{"sort",sort},{"filters",filters}};m_previousPages.clear();loadRecords();
}

void RelationalWorkbench::manageIndexes(){
    if(m_blocked || m_table.isEmpty())return;QJsonObject table;for(const auto &entry:m_catalog["tables"].toArray())if(entry.toObject()["id"].toString()==m_table)table=entry.toObject();if(table.isEmpty())return;
    const auto reviewed=m_info;QDialog dialog(this);dialog.setWindowTitle(tr("Native table indexes"));QVBoxLayout layout(&dialog);QListWidget list;list.setObjectName("tableIndexList");layout.addWidget(&list);
    for(const auto &col:table["columns"].toArray())if(col.toObject()["unique"].toBool())new QListWidgetItem(tr("UNIQUE %1 — owned by column constraint").arg(col.toObject()["name"].toString()),&list);
    for(const auto &entry:m_catalog["indexes"].toArray()){const auto index=entry.toObject();if(index["table"]!=table["id"])continue;QStringList keys;for(const auto &key:index["columns"].toArray())for(const auto &col:table["columns"].toArray())if(col.toObject()["id"]==key.toObject()["column"])keys.append(col.toObject()["name"].toString()+" "+key.toObject()["direction"].toString());auto *item=new QListWidgetItem(index["name"].toString()+" ("+keys.join(", ")+") "+(index["unique"].toBool()?tr("UNIQUE "):QString{})+tr("owner: ")+index["owner"].toString(),&list);if(index["owner"]=="user")item->setData(Qt::UserRole,index["id"]);}
    QPushButton add(tr("Create index…")),drop(tr("Drop selected user index…"));add.setObjectName("createTableIndex");drop.setObjectName("dropTableIndex");add.setEnabled(!m_locked);drop.setEnabled(!m_locked);layout.addWidget(&add);layout.addWidget(&drop);QDialogButtonBox close(QDialogButtonBox::Close);layout.addWidget(&close);connect(&close,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    connect(&drop,&QPushButton::clicked,&dialog,[this,&dialog,&list,reviewed]{if(!list.currentItem() || !list.currentItem()->data(Qt::UserRole).isValid())return;const auto id=list.currentItem()->data(Qt::UserRole).toJsonValue();dialog.accept();emit submitRequested({{"action","index_drop"},{"index",id},{"workbook",reviewed["id"]},{"expected_state",reviewed["state"]}});});
    connect(&add,&QPushButton::clicked,&dialog,[this,&dialog,table,reviewed]{
        bool ok=false;const auto name=QInputDialog::getText(&dialog,tr("Index SQL name"),tr("Name"),QLineEdit::Normal,{},&ok);if(!ok)return;const auto columns=table["columns"].toArray();ListCriteriaDialog keys({{"r1",1},{"r2",1},{"c1",1},{"c2",columns.size()},{"table_columns",columns}},&dialog);keys.setWindowTitle(tr("Choose index keys in priority order"));keys.findChild<QTableWidget *>("filterCriteria")->setEnabled(false);if(keys.exec()!=QDialog::Accepted || keys.scope()["sort"].toArray().isEmpty())return;
        const auto answer=QMessageBox::question(&dialog,tr("Unique index?"),tr("Require unique non-NULL key tuples? Creation clears session history and may fail on duplicate data. Choose No for a nonunique index."),QMessageBox::Yes|QMessageBox::No|QMessageBox::Cancel,QMessageBox::No);if(answer==QMessageBox::Cancel)return;QJsonArray entries;for(const auto &entry:keys.scope()["sort"].toArray()){const auto key=entry.toObject();entries.append(QJsonObject{{"column",columns[key["col"].toInt()-1].toObject()["id"]},{"direction",key["descending"].toBool()?"desc":"asc"}});}dialog.accept();emit submitRequested({{"action","index_create"},{"table",table["id"]},{"name",name},{"columns",entries},{"unique",answer==QMessageBox::Yes},{"workbook",reviewed["id"]},{"expected_state",reviewed["state"]}});
    });dialog.exec();
}

bool RelationalWorkbench::editDefinition(QWidget *parent,QJsonObject &definition,const QString &title,bool fixedWidth) {
    QDialog dialog(parent);dialog.setObjectName("tableDefinitionDialog");dialog.setWindowTitle(title);dialog.resize(640,400);
    auto *layout=new QVBoxLayout(&dialog);auto *form=new QFormLayout;auto *name=new QLineEdit(definition["name"].toString());
    name->setObjectName("tableName");name->setAccessibleName(tr("Table name"));name->setMaxLength(64);form->addRow(tr("Table name"),name);layout->addLayout(form);
    auto *columns=new QTableWidget(0,4);columns->setObjectName("columnDefinitions");columns->setAccessibleName(tr("Column definitions"));
    columns->setHorizontalHeaderLabels({tr("Column name"),tr("Type"),tr("Required"),tr("Unique")});columns->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);layout->addWidget(columns,1);
    auto append=[columns](const QJsonObject &column){
        const int row=columns->rowCount();columns->insertRow(row);auto *item=new QTableWidgetItem(column["name"].toString());item->setData(Qt::UserRole,column["id"]);columns->setItem(row,0,item);
        auto *type=new QComboBox;type->addItems({"text","integer","number","boolean","date","time","datetime"});type->setCurrentText(column["type"].toString("text"));type->setAccessibleName(tr("Column type"));columns->setCellWidget(row,1,type);
        for(int c=2;c<4;++c){auto *box=new QCheckBox;box->setChecked(column[c==2?"required":"unique"].toBool());box->setAccessibleName(c==2?tr("Required value"):tr("Unique value"));columns->setCellWidget(row,c,box);}
    };
    auto initial=definition["columns"].toArray();if(initial.isEmpty())initial.append(QJsonObject{{"name","Value"}});
    for(const auto &column:initial)append(column.toObject());
    auto *add=new QPushButton(tr("Add column"));add->setObjectName("addTableColumn");connect(add,&QPushButton::clicked,&dialog,[append,columns]{if(columns->rowCount()<256)append({});});layout->addWidget(add);
    auto *remove=new QPushButton(tr("Remove selected column"));remove->setObjectName("removeTableColumn");connect(remove,&QPushButton::clicked,&dialog,[columns]{if(columns->rowCount()>1 && columns->currentRow()>=0)columns->removeRow(columns->currentRow());});layout->addWidget(remove);add->setVisible(!fixedWidth);remove->setVisible(!fixedWidth);
    auto *notice=new QLabel(tr("Names use letters, digits and underscores, beginning with a letter. Tables use native format v4. Save a native copy before upgrading if an older reader is needed."));notice->setWordWrap(true);layout->addWidget(notice);
    auto *buttons=new QDialogButtonBox(QDialogButtonBox::Ok|QDialogButtonBox::Cancel);layout->addWidget(buttons);
    connect(buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    if(dialog.exec()!=QDialog::Accepted)return false;
    QJsonArray result;
    for(int row=0;row<columns->rowCount();++row){
        QJsonObject column{{"name",columns->item(row,0)->text()},{"type",qobject_cast<QComboBox *>(columns->cellWidget(row,1))->currentText()},
            {"required",qobject_cast<QCheckBox *>(columns->cellWidget(row,2))->isChecked()},{"unique",qobject_cast<QCheckBox *>(columns->cellWidget(row,3))->isChecked()}};
        const auto id=columns->item(row,0)->data(Qt::UserRole).toJsonValue();if(id.isString())column["id"]=id;
        result.append(column);
    }
    definition={{"name",name->text()},{"columns",result}};return true;
}

void RelationalWorkbench::createTable() {
    if(m_locked)return;const auto reviewed=m_info;
    if(!editDefinition(this,m_draftDefinition,tr("Create empty table")))return;
    if(reviewed["id"]!=m_info["id"] || reviewed["state"]!=m_info["state"]){emit issue(tr("Workbook changed. Review the table definition again."));return;}
    emit submitRequested({{"action","table_create"},{"name",m_draftDefinition["name"]},{"columns",m_draftDefinition["columns"]},
        {"workbook",reviewed["id"]},{"expected_state",reviewed["state"]}});
}

void RelationalWorkbench::dropTable() {
    if(m_locked || m_table.isEmpty())return;const auto reviewed=m_info;const auto table=m_table;
    if(QMessageBox::question(this,tr("Drop table"),tr("Permanently drop the selected table and all its records, and clear session Undo/Redo? The operation rejects if it exceeds the ordinary size limit."),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel)!=QMessageBox::Yes)return;
    emit submitRequested({{"action","table_drop"},{"table",table},{"allow_data_loss",true},{"workbook",reviewed["id"]},{"expected_state",reviewed["state"]}});
}

void RelationalWorkbench::editSchema() {
    if(m_locked || m_table.isEmpty())return;
    QJsonObject original;for(const auto &entry:m_catalog["tables"].toArray())if(entry.toObject()["id"].toString()==m_table)original=entry.toObject();
    if(original.isEmpty())return;
    const auto reviewed=m_info;const auto table=m_table;
    QJsonObject definition=m_schemaDraftTable==table && m_schemaDraftState==m_info["state"].toString()?m_schemaDraft:QJsonObject{{"name",original["name"]},{"columns",original["columns"]}};
    if(!editDefinition(this,definition,tr("Review table schema")))return;
    m_schemaDraft=definition;m_schemaDraftTable=table;m_schemaDraftState=reviewed["state"].toString();
    bool accepted=false;const QStringList policies{tr("Keep strict types"),tr("Convert using dot decimals and ISO dates")};
    const auto policy=QInputDialog::getItem(this,tr("Schema value conversion"),tr("Convert existing values"),policies,0,false,&accepted);
    if(!accepted || reviewed["id"]!=m_info["id"] || reviewed["state"]!=m_info["state"] || table!=m_table)return;
    QSet<QString> retained;for(const auto &entry:definition["columns"].toArray())if(entry.toObject()["id"].isString())retained.insert(entry.toObject()["id"].toString());
    QJsonArray removed;for(const auto &entry:original["columns"].toArray())if(!retained.contains(entry.toObject()["id"].toString()))removed.append(entry.toObject()["id"]);
    const QJsonObject request{{"table",table},{"name",definition["name"]},{"columns",definition["columns"]},{"drop_columns",removed},
        {"conversion_policy",policies.indexOf(policy)==0?"strict":"invariant"},{"expected_state",reviewed["state"]}};
    emit busyRequested(true);
    m_client->request(base()+"/tables/schema-preview",request,[this,reviewed,table](QJsonObject result,QString error){
        if(reviewed["id"]!=m_info["id"])return;
        if(!error.isEmpty() || reviewed["state"]!=m_info["state"] || table!=m_table){emit busyRequested(false);emit issue(error.isEmpty()?tr("Schema review changed. Review the table again."):error);return;}
        m_schemaDraft={{"name",result["table"].toObject()["name"]},{"columns",result["table"].toObject()["columns"]}};
        QDialog dialog(this);dialog.setObjectName("tableSchemaReview");dialog.setWindowTitle(tr("Review atomic schema conversion"));dialog.resize(700,420);QVBoxLayout layout(&dialog);
        QLabel summary(tr("Convert %1 records and apply the reviewed schema? Retained row/column identities persist. Session Undo/Redo clears.").arg(result["record_count"].toInt()));summary.setWordWrap(true);layout.addWidget(&summary);
        const auto columns=result["table"].toObject()["columns"].toArray(),rows=result["records_preview"].toArray();QTableWidget values(rows.size(),columns.size());values.setObjectName("schemaValuePreview");values.setAccessibleName(tr("Converted record values preview"));values.setEditTriggers(QAbstractItemView::NoEditTriggers);
        QStringList labels,identities;for(const auto &col:columns)labels.append(col.toObject()["name"].toString());values.setHorizontalHeaderLabels(labels);
        for(int r=0;r<rows.size();++r){const auto row=rows[r].toObject();identities.append(row["row_id"].toString());for(int c=0;c<columns.size();++c){const auto value=row["after"].toObject()[columns[c].toObject()["id"].toString()];values.setItem(r,c,new QTableWidgetItem(value.isNull()?tr("NULL"):value.toVariant().toString().left(256)));}}
        values.setVerticalHeaderLabels(identities);layout.addWidget(&values,1);
        QStringList report;for(const auto &line:result["report"].toArray())report.append(line.toString());for(const auto &entry:result["issues"].toArray()){const auto issue=entry.toObject();report.append(tr("Record %1, %2: %3").arg(issue["row_id"].toString(),issue["column"].toString(),issue["message"].toString()));}
        QLabel details(report.join('\n'));details.setWordWrap(true);layout.addWidget(&details);QDialogButtonBox buttons(QDialogButtonBox::Apply|QDialogButtonBox::Cancel);buttons.button(QDialogButtonBox::Apply)->setEnabled(result["can_apply"].toBool());layout.addWidget(&buttons);
        connect(buttons.button(QDialogButtonBox::Apply),&QPushButton::clicked,&dialog,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
        const auto answer=dialog.exec();emit busyRequested(false);
        if(answer!=QDialog::Accepted || !result["can_apply"].toBool() || reviewed["state"]!=m_info["state"] || reviewed["id"]!=m_info["id"] || table!=m_table)return;
        auto operation=result["source_request"].toObject();operation["action"]="table_schema";operation["review_digest"]=result["review_digest"];operation["workbook"]=reviewed["id"];operation["expected_state"]=reviewed["state"];emit submitRequested(operation);
    });
}
