// SPDX-License-Identifier: GPL-3.0-only
#include "relationalworkbench.h"
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
    auto *layout=new QVBoxLayout(this);auto *buttons=new QHBoxLayout;
    auto *sheets=new QPushButton(tr("Return to sheets"));sheets->setObjectName("returnToSheets");
    m_create=new QPushButton(tr("Create empty table…"));m_create->setObjectName("createTable");
    m_drop=new QPushButton(tr("Drop table…"));m_drop->setObjectName("dropTable");
    m_editSchema=new QPushButton(tr("Edit table columns…"));m_editSchema->setObjectName("editTableSchema");
    auto *refresh=new QPushButton(tr("Refresh tables"));refresh->setObjectName("refreshTables");
    for(auto *button:{sheets,m_create,m_editSchema,m_drop,refresh})buttons->addWidget(button);
    buttons->addStretch();layout->addLayout(buttons);
    auto *splitter=new QSplitter;layout->addWidget(splitter,1);
    m_objects=new QTreeWidget; m_objects->setObjectName("relationalObjects");m_objects->setHeaderLabel(tr("Workbook objects"));
    m_objects->setAccessibleName(tr("Workbook tables and queries"));splitter->addWidget(m_objects);
    m_schema=new QTableWidget(0,4);m_schema->setObjectName("tableSchema");m_schema->setAccessibleName(tr("Table columns"));
    m_schema->setHorizontalHeaderLabels({tr("Column"),tr("Type"),tr("Required"),tr("Unique")});
    m_schema->setEditTriggers(QAbstractItemView::NoEditTriggers);m_schema->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
    auto *tabs=new QTabWidget;tabs->setObjectName("tableViews");
    m_records=new QTableWidget; m_records->setObjectName("tableRecords");m_records->setAccessibleName(tr("Table records"));
    m_records->setEditTriggers(QAbstractItemView::NoEditTriggers);m_records->setSelectionBehavior(QAbstractItemView::SelectRows);
    tabs->addTab(m_records,tr("Records"));tabs->addTab(m_schema,tr("Columns"));splitter->addWidget(tabs);splitter->setStretchFactor(1,1);
    auto *paging=new QHBoxLayout;m_previous=new QPushButton(tr("Previous page"));m_previous->setObjectName("previousTablePage");
    m_next=new QPushButton(tr("Next page"));m_next->setObjectName("nextTablePage");paging->addWidget(m_previous);paging->addWidget(m_next);paging->addStretch();layout->addLayout(paging);
    connect(m_next,&QPushButton::clicked,this,[this]{if(!m_nextAfter.isEmpty()){m_previousPages.append(m_after);loadRecords(m_nextAfter);}});
    connect(m_previous,&QPushButton::clicked,this,[this]{if(!m_previousPages.isEmpty())loadRecords(m_previousPages.takeLast());});
    m_status=new QLabel(tr("Choose a table or create an empty table."));m_status->setObjectName("tableStatus");m_status->setWordWrap(true);
    m_status->setAccessibleName(tr("Table operation status"));layout->addWidget(m_status);
    connect(sheets,&QPushButton::clicked,this,&RelationalWorkbench::sheetsRequested);
    connect(refresh,&QPushButton::clicked,this,&RelationalWorkbench::refreshCatalog);
    connect(m_create,&QPushButton::clicked,this,&RelationalWorkbench::createTable);
    connect(m_drop,&QPushButton::clicked,this,&RelationalWorkbench::dropTable);
    connect(m_editSchema,&QPushButton::clicked,this,&RelationalWorkbench::editSchema);
    connect(m_objects,&QTreeWidget::currentItemChanged,this,[this](QTreeWidgetItem *item){if(item)showTable(item->data(0,Qt::UserRole).toString());});
    connect(m_client,&RestClient::configured,this,[this]{++m_generation;m_info={};m_catalog={};m_objects->clear();m_schema->setRowCount(0);setLocked(true);});
    setLocked(true);
}

QString RelationalWorkbench::base()const{return "/v1/workbooks/"+m_info["id"].toString();}

void RelationalWorkbench::bind(const QJsonObject &info) {
    if(m_info["id"]!=info["id"]) {m_table.clear();m_catalog={};m_draftDefinition={};}
    ++m_generation;++m_readGeneration;m_info=info;setLocked(info.isEmpty());
    if(isVisible() && !info.isEmpty())refreshCatalog();
}

void RelationalWorkbench::setLocked(bool locked) {
    m_blocked=locked || m_info.isEmpty();m_locked=m_blocked || m_info.value("readonly").toBool();
    m_create->setEnabled(!m_locked);m_drop->setEnabled(!m_locked && !m_table.isEmpty());
    m_editSchema->setEnabled(!m_locked && !m_table.isEmpty());
    m_previous->setEnabled(!m_blocked && !m_previousPages.isEmpty());m_next->setEnabled(!m_blocked && !m_nextAfter.isEmpty());
}

void RelationalWorkbench::refreshCatalog() {
    if(m_info.isEmpty())return;
    const auto generation=++m_generation;const auto state=m_info["state"];
    m_status->setText(tr("Loading table catalog…"));
    m_client->request(base()+"/tables",{{"expected_state",state}},[this,generation,state](QJsonObject result,QString error){
        if(generation!=m_generation)return;
        if(!error.isEmpty()){m_status->setText(error);emit issue(error);return;}
        if(result["state"]!=state){m_status->setText(tr("Catalog changed. Refresh the workbook."));return;}
        m_catalog=result;m_objects->blockSignals(true);m_objects->clear();
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
    if(!id.isEmpty())loadRecords();
}

void RelationalWorkbench::loadRecords(const QString &after) {
    if(m_table.isEmpty() || m_info.isEmpty())return;
    const auto generation=++m_readGeneration;const auto table=m_table;const auto state=m_info["state"];
    QJsonObject request{{"table",table},{"expected_state",state}};if(!after.isEmpty())request["after"]=after;
    m_status->setText(tr("Loading records…"));m_client->request(base()+"/tables/read",request,[this,generation,table,state,after](QJsonObject result,QString error){
        if(generation!=m_readGeneration || m_table!=table || state!=m_info["state"])return;
        if(!error.isEmpty()){m_status->setText(error);emit issue(error);return;}
        if(result["state"]!=state || result["table"]!=table){m_status->setText(tr("Table page changed; refresh the workbook."));return;}
        m_page=result;m_after=after;m_nextAfter=result["next_after"].toString();
        const auto cols=result["columns"].toArray(),rows=result["records"].toArray();m_records->clear();m_records->setRowCount(rows.size());m_records->setColumnCount(cols.size());QStringList labels;
        for(const auto &col:cols)labels.append(col.toObject()["name"].toString());m_records->setHorizontalHeaderLabels(labels);
        for(int r=0;r<rows.size();++r){const auto record=rows[r].toObject();for(int c=0;c<cols.size();++c){const auto value=record["values"].toObject()[cols[c].toObject()["id"].toString()];
            auto *item=new QTableWidgetItem(value.isNull()?tr("NULL"):value.isBool()?(value.toBool()?tr("TRUE"):tr("FALSE")):value.toVariant().toString());item->setData(Qt::UserRole,record["row_id"]);m_records->setItem(r,c,item);}}
        m_status->setText(rows.isEmpty()?tr("No records."):tr("%1 records on this page. Record identity is separate from row position.").arg(rows.size()));setLocked(m_blocked);
    });
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
