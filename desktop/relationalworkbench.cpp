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
#include <QMessageBox>
#include <QPushButton>
#include <QSplitter>
#include <QTableWidget>
#include <QTreeWidget>
#include <QVBoxLayout>

RelationalWorkbench::RelationalWorkbench(RestClient *client,QWidget *parent):QWidget(parent),m_client(client) {
    setObjectName("relationalWorkbench");
    auto *layout=new QVBoxLayout(this);auto *buttons=new QHBoxLayout;
    auto *sheets=new QPushButton(tr("Return to sheets"));sheets->setObjectName("returnToSheets");
    m_create=new QPushButton(tr("Create empty table…"));m_create->setObjectName("createTable");
    m_drop=new QPushButton(tr("Drop empty table…"));m_drop->setObjectName("dropTable");
    auto *refresh=new QPushButton(tr("Refresh tables"));refresh->setObjectName("refreshTables");
    for(auto *button:{sheets,m_create,m_drop,refresh})buttons->addWidget(button);
    buttons->addStretch();layout->addLayout(buttons);
    auto *splitter=new QSplitter;layout->addWidget(splitter,1);
    m_objects=new QTreeWidget; m_objects->setObjectName("relationalObjects");m_objects->setHeaderLabel(tr("Workbook objects"));
    m_objects->setAccessibleName(tr("Workbook tables and queries"));splitter->addWidget(m_objects);
    m_schema=new QTableWidget(0,4);m_schema->setObjectName("tableSchema");m_schema->setAccessibleName(tr("Table columns"));
    m_schema->setHorizontalHeaderLabels({tr("Column"),tr("Type"),tr("Required"),tr("Unique")});
    m_schema->setEditTriggers(QAbstractItemView::NoEditTriggers);m_schema->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);
    splitter->addWidget(m_schema);splitter->setStretchFactor(1,1);
    m_status=new QLabel(tr("Choose a table or create an empty table."));m_status->setObjectName("tableStatus");m_status->setWordWrap(true);
    m_status->setAccessibleName(tr("Table operation status"));layout->addWidget(m_status);
    connect(sheets,&QPushButton::clicked,this,&RelationalWorkbench::sheetsRequested);
    connect(refresh,&QPushButton::clicked,this,&RelationalWorkbench::refreshCatalog);
    connect(m_create,&QPushButton::clicked,this,&RelationalWorkbench::createTable);
    connect(m_drop,&QPushButton::clicked,this,&RelationalWorkbench::dropTable);
    connect(m_objects,&QTreeWidget::currentItemChanged,this,[this](QTreeWidgetItem *item){if(item)showTable(item->data(0,Qt::UserRole).toString());});
    connect(m_client,&RestClient::configured,this,[this]{++m_generation;m_info={};m_catalog={};m_objects->clear();m_schema->setRowCount(0);setLocked(true);});
    setLocked(true);
}

QString RelationalWorkbench::base()const{return "/v1/workbooks/"+m_info["id"].toString();}

void RelationalWorkbench::bind(const QJsonObject &info) {
    if(m_info["id"]!=info["id"]) {m_table.clear();m_catalog={};m_draftDefinition={};}
    ++m_generation;m_info=info;setLocked(info.isEmpty() || info["readonly"].toBool());
    if(isVisible() && !info.isEmpty())refreshCatalog();
}

void RelationalWorkbench::setLocked(bool locked) {
    m_locked=locked || m_info.isEmpty() || m_info["readonly"].toBool();
    m_create->setEnabled(!m_locked);m_drop->setEnabled(!m_locked && !m_table.isEmpty());
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
    m_table=id;m_schema->setRowCount(0);
    for(const auto &entry:m_catalog["tables"].toArray()) {
        const auto table=entry.toObject();if(table["id"].toString()!=id)continue;
        for(const auto &entry:table["columns"].toArray()) {
            const auto col=entry.toObject();const int row=m_schema->rowCount();m_schema->insertRow(row);
            const QStringList values{col["name"].toString(),col["type"].toString(),col["required"].toBool()?tr("Yes"):tr("No"),col["unique"].toBool()?tr("Yes"):tr("No")};
            for(int c=0;c<values.size();++c)m_schema->setItem(row,c,new QTableWidgetItem(values[c]));
        }
    }
    setLocked(m_locked);
}

bool RelationalWorkbench::editDefinition(QWidget *parent,QJsonObject &definition,const QString &title) {
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
    if(QMessageBox::question(this,tr("Drop empty table"),tr("Drop the selected empty table and clear session Undo/Redo?"),QMessageBox::Yes|QMessageBox::Cancel,QMessageBox::Cancel)!=QMessageBox::Yes)return;
    emit submitRequested({{"action","table_drop"},{"table",table},{"workbook",reviewed["id"]},{"expected_state",reviewed["state"]}});
}
