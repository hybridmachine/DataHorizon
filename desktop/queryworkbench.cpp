// SPDX-License-Identifier: GPL-3.0-only
#include "queryworkbench.h"
#include "recordinputdialog.h"
#include "relationalexport.h"
#include <QApplication>
#include <QClipboard>
#include <QCheckBox>
#include <QComboBox>
#include <QHeaderView>
#include <QJsonDocument>
#include <QLabel>
#include <QListWidget>
#include <QMimeData>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QSplitter>
#include <QTableWidget>
#include <QTimer>
#include <QTreeWidget>
#include <QUuid>
#include <QVBoxLayout>
#include <QShortcut>
#include <QFileInfo>

static QString sqlName(QString name){name.replace('"',"\"\"");return '"'+name+'"';}
static QString resultText(const QJsonValue &value){return value.isObject() && value.toObject()["type"]=="integer"?value.toObject()["value"].toString():tableValueText(value);}

QueryWorkbench::QueryWorkbench(RestClient *client,QWidget *parent):QWidget(parent),m_client(client){
    setObjectName("queryWorkbench");auto *layout=new QVBoxLayout(this);m_source=new QLabel; m_source->setWordWrap(true);layout->addWidget(m_source);
    auto *definitions=new QHBoxLayout;m_savedName=new QLabel(tr("Unsaved SQL"));m_savedName->setObjectName("savedQueryName");definitions->addWidget(m_savedName,1);m_save=new QPushButton(tr("Save definition…"));m_save->setObjectName("saveQueryDefinition");m_saveAs=new QPushButton(tr("Save as…"));m_saveAs->setObjectName("saveQueryAs");m_delete=new QPushButton(tr("Delete saved definition…"));m_delete->setObjectName("deleteSavedQuery");for(auto *button:{m_save,m_saveAs,m_delete})definitions->addWidget(button);layout->addLayout(definitions);connect(m_save,&QPushButton::clicked,this,[this]{saveDefinition();});connect(m_saveAs,&QPushButton::clicked,this,[this]{saveDefinition(true);});connect(m_delete,&QPushButton::clicked,this,&QueryWorkbench::deleteDefinition);
    auto *splitter=new QSplitter;layout->addWidget(splitter,1);m_schema=new QTreeWidget;m_schema->setHeaderLabel(tr("SQL schema — double-click to insert"));m_schema->setObjectName("querySchema");m_schema->setAccessibleName(tr("Registered SQL tables and columns"));splitter->addWidget(m_schema);
    auto *editor=new QWidget;auto *form=new QVBoxLayout(editor);m_sql=new QPlainTextEdit;m_sql->setObjectName("querySql");m_sql->setAccessibleName(tr("Read-only SQL editor"));m_sql->setTabChangesFocus(true);m_sql->setPlaceholderText(tr("SELECT ... FROM ... WHERE key = :parameter"));form->addWidget(m_sql,2);
    m_parameters=new QTableWidget(0,3);m_parameters->setObjectName("queryParameters");m_parameters->setAccessibleName(tr("Named parameter declarations"));m_parameters->setHorizontalHeaderLabels({tr("Name after colon"),tr("Type"),tr("Allow NULL")});m_parameters->horizontalHeader()->setSectionResizeMode(QHeaderView::Stretch);form->addWidget(m_parameters,1);
    auto *parameterButtons=new QHBoxLayout;auto *add=new QPushButton(tr("Add parameter")),*remove=new QPushButton(tr("Remove parameter"));add->setObjectName("addQueryParameter");remove->setObjectName("removeQueryParameter");parameterButtons->addWidget(add);parameterButtons->addWidget(remove);form->addLayout(parameterButtons);connect(add,&QPushButton::clicked,this,[this]{addParameter();});connect(remove,&QPushButton::clicked,this,[this]{if(m_parameters->currentRow()>=0)m_parameters->removeRow(m_parameters->currentRow());});splitter->addWidget(editor);splitter->setStretchFactor(1,3);
    auto *actions=new QHBoxLayout;m_run=new QPushButton(tr("Run / Refresh"));m_run->setObjectName("runQuery");m_cancel=new QPushButton(tr("Cancel / Discard result"));m_cancel->setObjectName("cancelQuery");m_columns=new QPushButton(tr("Displayed columns…"));m_columns->setObjectName("queryChooseColumns");m_copy=new QPushButton(tr("Copy selected values"));m_copy->setObjectName("copyQueryValues");for(auto *button:{m_run,m_cancel,m_columns,m_copy})actions->addWidget(button);layout->addLayout(actions);
    auto *exports=new QHBoxLayout;m_export=new QPushButton(tr("Export complete result…"));m_export->setObjectName("exportQueryResult");exports->addWidget(m_export);auto *refresh=new QPushButton(tr("Refresh stored data"));refresh->setObjectName("refreshQuerySource");exports->addWidget(refresh);exports->addStretch();layout->addLayout(exports);connect(refresh,&QPushButton::clicked,this,&QueryWorkbench::refreshRequested);connect(m_export,&QPushButton::clicked,this,[this]{const auto target=RelationalExport::destination(this);if(!target.first.isEmpty())exportTo(target.first,target.second);});
    m_grid=new QTableWidget;m_grid->setObjectName("queryResults");m_grid->setAccessibleName(tr("Read-only query result page"));m_grid->setEditTriggers(QAbstractItemView::NoEditTriggers);layout->addWidget(m_grid,2);
    auto *paging=new QHBoxLayout;m_previous=new QPushButton(tr("Previous page"));m_previous->setObjectName("previousQueryPage");m_next=new QPushButton(tr("Next page"));m_next->setObjectName("nextQueryPage");paging->addWidget(m_previous);paging->addWidget(m_next);paging->addStretch();layout->addLayout(paging);m_status=new QLabel(tr("Run one read-only SELECT or CTE. Results are private, frozen and ephemeral."));m_status->setObjectName("queryStatus");m_status->setAccessibleName(tr("SQL execution and result status"));m_status->setWordWrap(true);m_status->setTextInteractionFlags(Qt::TextSelectableByMouse);layout->addWidget(m_status);
    connect(m_run,&QPushButton::clicked,this,&QueryWorkbench::run);connect(m_cancel,&QPushButton::clicked,this,&QueryWorkbench::cancel);connect(m_columns,&QPushButton::clicked,this,&QueryWorkbench::chooseColumns);connect(m_copy,&QPushButton::clicked,this,&QueryWorkbench::copyValues);
    auto *runKey=new QShortcut(QKeySequence(Qt::CTRL|Qt::Key_Return),this);runKey->setContext(Qt::WidgetWithChildrenShortcut);connect(runKey,&QShortcut::activated,m_run,&QPushButton::click);
    connect(m_next,&QPushButton::clicked,this,[this]{if(m_page.value("next_offset").isDouble()){m_previousPages.append(m_page.value("offset").toInt());page(m_page.value("next_offset").toInt());}});connect(m_previous,&QPushButton::clicked,this,[this]{if(!m_previousPages.isEmpty())page(m_previousPages.takeLast());});
    connect(m_schema,&QTreeWidget::itemExpanded,this,[this](QTreeWidgetItem *node){
        if(node->parent())return;for(int i=0;i<m_schema->topLevelItemCount();++i){auto *other=m_schema->topLevelItem(i);if(other!=node){other->setExpanded(false);qDeleteAll(other->takeChildren());}}
        if(node->childCount())return;for(const auto &entry:m_catalog.value("tables").toArray()){const auto table=entry.toObject();if(table["id"]!=node->data(0,Qt::UserRole+1).toJsonValue())continue;auto *identity=new QTreeWidgetItem(node,{"_dh_row_id (identity)"});identity->setData(0,Qt::UserRole,sqlName(table["name"].toString())+".\"_dh_row_id\"");for(const auto &column:table["columns"].toArray()){const auto col=column.toObject();auto *child=new QTreeWidgetItem(node,{col["name"].toString()+" ("+col["type"].toString()+")"});child->setData(0,Qt::UserRole,sqlName(table["name"].toString())+"."+sqlName(col["name"].toString()));}}
    });
    connect(m_schema,&QTreeWidget::itemDoubleClicked,this,[this](QTreeWidgetItem *item){m_sql->insertPlainText(item->data(0,Qt::UserRole).toString());m_sql->setFocus();});
    connect(m_sql,&QPlainTextEdit::textChanged,this,&QueryWorkbench::draftChanged);connect(m_parameters,&QTableWidget::itemChanged,this,&QueryWorkbench::draftChanged);connect(m_parameters->model(),&QAbstractItemModel::rowsRemoved,this,&QueryWorkbench::draftChanged);connect(m_parameters->model(),&QAbstractItemModel::rowsInserted,this,&QueryWorkbench::draftChanged);
    connect(m_client,&RestClient::configured,this,[this]{++m_generation;++m_pageGeneration;m_job.clear();m_running=false;m_startPending=false;m_result={};suspendView();m_info={};setLocked(true);});buttons();
}
QString QueryWorkbench::base()const{return "/v1/workbooks/"+m_info.value("id").toString()+"/queries/jobs";}
void QueryWorkbench::bind(const QJsonObject &info){
    if(m_info.value("id")!=info["id"]){++m_generation;m_job.clear();m_result={};m_running=false;m_startPending=false;m_catalog={};m_schema->clear();m_saved={};m_savedDigest.clear();suspendView();}
    else if(m_info.value("state")!=info["state"]){m_result={};suspendView();m_status->setText(tr("Stored inputs changed. Rerun explicitly with reviewed parameter values."));}
    m_info=info;m_source->setText(tr("Read-only SQL — %1").arg(QFileInfo(info["path"].toString()).fileName()));m_source->setToolTip(info["path"].toString());m_source->setAccessibleDescription(info["path"].toString());buttons();
}
void QueryWorkbench::setCatalog(const QJsonObject &catalog){
    m_catalog=catalog;m_schema->clear();for(const auto &entry:catalog["tables"].toArray()){const auto table=entry.toObject();auto *node=new QTreeWidgetItem(m_schema,{table["name"].toString()});node->setData(0,Qt::UserRole,sqlName(table["name"].toString()));node->setData(0,Qt::UserRole+1,table["id"]);node->setChildIndicatorPolicy(QTreeWidgetItem::ShowIndicator);}
}

void QueryWorkbench::setLocked(bool locked){m_locked=locked;buttons();}
void QueryWorkbench::draftChanged(){++m_editorRevision;m_result={};suspendView();if(!m_job.isEmpty())m_status->setText(tr("SQL or parameter declarations changed. Run explicitly to produce output for this draft."));}
void QueryWorkbench::buttons(){
    m_run->setEnabled(!m_locked && !m_info.isEmpty() && !m_info.value("repair").toBool() && !m_running && !m_closing);m_cancel->setEnabled(!m_job.isEmpty() && !m_startPending);m_columns->setEnabled(!m_result.isEmpty() && !m_closing);m_copy->setEnabled(!m_page.isEmpty() && !m_closing);m_previous->setEnabled(!m_previousPages.isEmpty() && !m_closing);m_next->setEnabled(m_page.value("next_offset").isDouble() && !m_closing);
    const bool writable=!m_locked && !m_info.isEmpty() && !m_info.value("readonly").toBool() && !m_info.value("repair").toBool();m_save->setEnabled(writable);m_saveAs->setEnabled(writable);m_delete->setEnabled(writable && !m_saved.isEmpty());m_export->setEnabled(!m_locked && !m_result.isEmpty() && !m_closing);m_savedName->setText(m_saved.isEmpty()?tr("Unsaved SQL"):m_saved.value("name").toString());
}
void QueryWorkbench::addParameter(const QJsonObject &parameter){
    if(m_parameters->rowCount()>=64)return;const int row=m_parameters->rowCount();m_parameters->insertRow(row);m_parameters->setItem(row,0,new QTableWidgetItem(parameter["name"].toString("parameter"+QString::number(row+1))));auto *type=new QComboBox;type->addItems({"text","integer","number","boolean","date","time","datetime"});type->setCurrentText(parameter["type"].toString("text"));type->setAccessibleName(tr("Parameter %1 type").arg(row+1));m_parameters->setCellWidget(row,1,type);auto *nullable=new QCheckBox;nullable->setChecked(parameter["nullable"].toBool());nullable->setAccessibleName(tr("Parameter %1 allows NULL").arg(row+1));m_parameters->setCellWidget(row,2,nullable);connect(type,&QComboBox::currentTextChanged,this,&QueryWorkbench::draftChanged);connect(nullable,&QCheckBox::toggled,this,&QueryWorkbench::draftChanged);
}
QJsonObject QueryWorkbench::definition()const{
    QJsonArray parameters;for(int row=0;row<m_parameters->rowCount();++row)parameters.append(QJsonObject{{"name",m_parameters->item(row,0)->text()},{"type",qobject_cast<QComboBox *>(m_parameters->cellWidget(row,1))->currentText()},{"nullable",qobject_cast<QCheckBox *>(m_parameters->cellWidget(row,2))->isChecked()}});return {{"sql",m_sql->toPlainText()},{"parameters",parameters}};
}
void QueryWorkbench::setDefinition(const QJsonObject &definition){m_sql->setPlainText(definition["sql"].toString());m_parameters->setRowCount(0);for(const auto &parameter:definition["parameters"].toArray())addParameter(parameter.toObject());m_values=definition["defaults"].toObject();m_result={};suspendView();}
void QueryWorkbench::run(){
    if(m_locked || m_running || m_info.isEmpty())return;auto request=definition();const QJsonValue declarations=request.value("parameters");
    if(!m_saved.isEmpty() && request["sql"]==m_saved.value("sql") && request["parameters"]==m_saved.value("parameters")){request={{"query",m_saved.value("id")},{"definition_digest",m_savedDigest}};}
    request["expected_state"]=m_info.value("state");request["job_id"]=QUuid::createUuid().toString(QUuid::WithoutBraces);
    const auto parameters=declarations.toArray();if(parameters.isEmpty()){request["values"]=QJsonObject{};start(request);return;}
    QJsonArray fields;for(const auto &parameter:parameters){const auto p=parameter.toObject();fields.append(QJsonObject{{"id",p["name"]},{"name",p["name"]},{"type",p["type"]},{"required",!p["nullable"].toBool()}});}
    RecordInputDialog editor(fields,m_values,this);editor.setObjectName("queryParameterValues");editor.setWindowTitle(tr("Review bound SQL parameters"));editor.explain(tr("Values are bound separately from SQL. Numbers use dot decimals, dates/times use ISO text, and NULL is explicit. These invocation values remain session drafts unless defaults are explicitly saved."));
    editor.applyRequested=[this,&editor,request]()mutable{m_values=editor.values();request["values"]=m_values;editor.pending(true);start(request,&editor);};editor.inspectRequested=[this]{poll();};editor.exec();m_parameterEditor=nullptr;
}
void QueryWorkbench::start(QJsonObject request,QPointer<RecordInputDialog> editor){
    if(m_locked || m_running)return;if(request["expected_state"]!=m_info.value("state")){if(editor)editor->stale(tr("Stored data changed. Close this review and run again."));return;}
    if(!m_job.isEmpty()){
        const auto job=m_job,root=base();m_client->request(root+"/"+job+"/release",{},[this,request,editor,job](QJsonObject,QString error){if(m_job!=job)return;if(!error.isEmpty()){if(editor)editor->error(error);else m_status->setText(error);return;}m_job.clear();start(request,editor);});return;
    }
    m_runRevision=m_editorRevision;m_parameterEditor=editor;m_result={};m_projection={};suspendView();m_job=request["job_id"].toString();m_running=true;m_startPending=true;buttons();m_status->setText(tr("Preparing private query snapshot…"));const auto generation=m_generation;
    m_client->request(base(),request,[this,generation,editor](QJsonObject result,QString error){
        if(generation!=m_generation)return;m_startPending=false;
        if(!error.isEmpty()){
            if(!result.isEmpty() && result["code"].isString()){m_running=false;m_job.clear();if(editor)editor->error(error);m_status->setText(error);buttons();if(m_closing)releaseForClose();return;}
            m_status->setText(tr("Query admission reply unavailable. Inspecting its ID; Cancel remains available."));
            if(editor)editor->unknown(tr("Query admission reply unavailable. Inspect its ID or close this parameter draft and use Cancel."));
        }else{if(editor)editor->accept();}
        buttons();if(m_closing)cancel();else poll();
    });
}
void QueryWorkbench::poll(){
    if(m_job.isEmpty() || m_startPending)return;const auto job=m_job;const auto generation=m_generation;
    m_client->request(base()+"/"+job,{},[this,job,generation](QJsonObject result,QString error){
        if(generation!=m_generation || job!=m_job)return;
        if(!error.isEmpty()){m_status->setText(error);if(m_closing){if(result["code"]=="JOB_NOT_FOUND"){m_job.clear();releaseForClose();}else{m_closing=false;auto done=std::move(m_closeCompletion);if(done)done(error);}}buttons();return;}
        if(result["job_id"].toString()!=job || !result["state"].isString() || !result["generation"].isDouble()){m_status->setText(tr("Invalid query status response. Inspect or cancel the captured job ID."));return;}
        if(m_parameterEditor)m_parameterEditor->accept();
        m_status->setText(tr("%1 — %2 rows processed. Job %3").arg(result["phase"].toString()).arg(result["rows_processed"].toInt()).arg(job));
        if(result["status"]=="running"){QTimer::singleShot(80,this,&QueryWorkbench::poll);return;}terminal(result);
    },false);
}
void QueryWorkbench::terminal(const QJsonObject &status){
    if(status["status"]=="completed" && (status["state"]!=m_info.value("state") || m_runRevision!=m_editorRevision)){auto stale=status;stale["status"]="stale";stale.remove("result");terminal(stale);return;}
    m_running=false;m_result=status["status"]=="completed"?status["result"].toObject():QJsonObject{};
    if(!m_result.isEmpty()){m_result["state"]=status["state"];m_result["generation"]=status["generation"];m_status->setText(tr("Complete: %1 rows, %2 values. SQL results are read-only; column aliases do not imply types.").arg(m_result.value("row_count").toInt()).arg(m_result.value("value_count").toInt()));if(isVisible() && !m_closing)page();}
    else{m_status->setText(status["status"].toString()+": "+status["error"].toObject()["message"].toString());suspendView();}
    buttons();if(m_closing)releaseForClose();
}
void QueryWorkbench::page(int offset){
    if(m_result.isEmpty() || m_result.value("state")!=m_info.value("state") || m_job.isEmpty())return;const auto generation=++m_pageGeneration;const auto job=m_job;const auto state=m_info.value("state");
    QJsonObject request{{"offset",offset},{"preview",true},{"limit",qMin(128,2048/qMax(1,m_projection.isEmpty()?16:int(m_projection.size())))},{"expected_state",state}};if(!m_projection.isEmpty())request["columns"]=m_projection;
    m_client->request(base()+"/"+job+"/page",request,[this,generation,job,state,offset](QJsonObject result,QString error){
        if(generation!=m_pageGeneration || job!=m_job || state!=m_info.value("state"))return;if(!error.isEmpty()){m_status->setText(error);m_result={};suspendView();buttons();return;}
        const auto columns=result["columns"].toArray(),rows=result["rows"].toArray();
        bool valid=result["job_id"].toString()==job && result["state"]==state && result["generation"]==m_result.value("generation") && !columns.isEmpty() && columns.size()<=256 && rows.size()<=128 && columns.size()*rows.size()<=2048;
        for(const auto &row:rows)valid=valid && row.isObject() && row.toObject()["values"].isArray() && row.toObject()["values"].toArray().size()==columns.size();
        if(!valid){m_status->setText(tr("Invalid or mismatched query page response; no result displayed."));return;}
        const auto charge=3*(QJsonDocument(m_catalog).toJson(QJsonDocument::Compact).size()+QJsonDocument(result).toJson(QJsonDocument::Compact).size()+QJsonDocument(m_result).toJson(QJsonDocument::Compact).size())+512*rows.size()*columns.size();if(charge>16*1024*1024){m_status->setText(tr("Choose fewer columns to stay within the desktop cache budget."));return;}
        m_page=result;m_page["offset"]=offset;m_grid->clear();m_grid->setRowCount(rows.size());m_grid->setColumnCount(columns.size());QStringList labels;for(const auto &col:columns)labels.append(QString::number(col.toObject()["id"].toString().toInt()+1)+": "+col.toObject()["name"].toString());m_grid->setHorizontalHeaderLabels(labels);
        for(int r=0;r<rows.size();++r){const auto row=rows[r].toObject();for(int c=0;c<columns.size();++c){const bool omitted=row["omitted_columns"].toArray().contains(columns[c].toObject()["id"]);auto *cell=new QTableWidgetItem(resultText(row["values"].toArray()[c])+(omitted?"…":""));cell->setFlags(cell->flags()&~Qt::ItemIsEditable);cell->setToolTip(omitted?tr("Text preview. Copy retrieves the full frozen value."):tr("Read-only result row %1; no editable record identity.").arg(row["row"].toInt()+1));m_grid->setItem(r,c,cell);}}buttons();
    });
}
void QueryWorkbench::suspendView(){++m_pageGeneration;m_page={};m_grid->setRowCount(0);m_grid->setColumnCount(0);m_previousPages.clear();buttons();}
void QueryWorkbench::resumeView(){if(!m_result.isEmpty() && !m_running)page();}
QJsonObject QueryWorkbench::cacheMetrics()const{return {{"estimated_bytes",3*(QJsonDocument(m_page).toJson(QJsonDocument::Compact).size()+QJsonDocument(m_result).toJson(QJsonDocument::Compact).size())+512*m_grid->rowCount()*m_grid->columnCount()},{"loaded_values",m_grid->rowCount()*m_grid->columnCount()}};}
void QueryWorkbench::chooseColumns(){
    if(m_result.isEmpty())return;QDialog dialog(this);dialog.setWindowTitle(tr("Displayed result columns"));QVBoxLayout layout(&dialog);QListWidget list;for(int i=0;i<m_result.value("columns").toArray().size();++i){const auto col=m_result.value("columns").toArray()[i].toObject();auto *item=new QListWidgetItem(QString::number(i+1)+": "+col["name"].toString(),&list);item->setFlags(item->flags()|Qt::ItemIsUserCheckable);item->setCheckState((m_projection.isEmpty()?i<16:m_projection.contains(col["id"]))?Qt::Checked:Qt::Unchecked);item->setData(Qt::UserRole,col["id"]);}layout.addWidget(&list);QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel);layout.addWidget(&buttons);connect(&buttons,&QDialogButtonBox::accepted,&dialog,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);if(dialog.exec()!=QDialog::Accepted)return;QJsonArray selected;for(int i=0;i<list.count();++i)if(list.item(i)->checkState()==Qt::Checked)selected.append(list.item(i)->data(Qt::UserRole).toJsonValue());if(selected.isEmpty())return;m_projection=selected;m_previousPages.clear();page();
}
void QueryWorkbench::copyValues(){
    const auto ranges=m_grid->selectedRanges();if(ranges.size()!=1 || m_page.isEmpty())return;const auto range=ranges.first();const auto job=m_job;const auto state=m_info.value("state");QJsonArray columns;for(int c=range.leftColumn();c<=range.rightColumn();++c)columns.append(m_page.value("columns").toArray()[c].toObject()["id"]);
    m_client->request(base()+"/"+job+"/page",{{"offset",m_page.value("offset").toInt()+range.topRow()},{"limit",range.rowCount()},{"columns",columns},{"expected_state",state}},[this,job,state](QJsonObject result,QString error){if(job!=m_job || state!=m_info.value("state"))return;if(!error.isEmpty()){emit issue(error);return;}QJsonArray values;QStringList lines;for(const auto &row:result["rows"].toArray()){const auto fields=row.toObject()["values"].toArray();values.append(fields);QStringList line;for(const auto &field:fields)line.append(field.isNull()?QString{}:resultText(field));lines.append(line.join('\t'));}auto *mime=new QMimeData;mime->setData("application/x-datahorizon-table-values",QJsonDocument(QJsonObject{{"version",1},{"rows",values}}).toJson(QJsonDocument::Compact));mime->setText(lines.join('\n'));QApplication::clipboard()->setMimeData(mime);});
}
void QueryWorkbench::cancel(){
    if(m_job.isEmpty() || m_startPending)return;const auto job=m_job;m_client->request(base()+"/"+job+"/cancel",{},[this,job](QJsonObject result,QString error){if(job!=m_job)return;if(!error.isEmpty()){m_status->setText(error);if(result["code"]=="JOB_NOT_FOUND"){m_job.clear();m_running=false;m_result={};suspendView();if(m_closing)releaseForClose();}return;}if(result["status"]=="running")poll();else terminal(result);});
}
void QueryWorkbench::closeJob(std::function<void(QString)> completion){m_closing=true;m_closeCompletion=std::move(completion);buttons();if(m_startPending)return;if(m_job.isEmpty())releaseForClose();else cancel();}
void QueryWorkbench::releaseForClose(){
    if(m_job.isEmpty()){m_running=false;m_closing=false;auto done=std::move(m_closeCompletion);buttons();if(done)done({});return;}
    const auto job=m_job;m_client->request(base()+"/"+job+"/release",{},[this,job](QJsonObject result,QString error){if(job!=m_job)return;if(error.isEmpty() || result["code"]=="JOB_NOT_FOUND"){m_job.clear();m_result={};suspendView();error.clear();}m_closing=false;auto done=std::move(m_closeCompletion);buttons();if(done)done(error);});
}
