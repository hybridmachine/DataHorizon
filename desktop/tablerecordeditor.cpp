// SPDX-License-Identifier: GPL-3.0-only
#include "relationalworkbench.h"
#include "recordinputdialog.h"
#include "queryworkbench.h"
#include "clipboarddata.h"
#include <QApplication>
#include <QClipboard>
#include <QJsonDocument>
#include <QMimeData>
#include <QTableWidget>
#include <QTimer>
#include <algorithm>

bool RelationalWorkbench::hasActiveEditor()const{return !m_recordDialog.isNull();}
void RelationalWorkbench::restoreDraft(const QJsonObject &draft){m_recordDraft=draft;setLocked(m_blocked);}
void RelationalWorkbench::operationAdmitted(const QString &operation){
    m_query->operationAdmitted(operation);
    if(!m_awaitingDataAdmission)return;
    m_awaitingDataAdmission=false;m_dataOperation=operation;m_recordDraft["operation_id"]=operation;
}
void RelationalWorkbench::admissionRejected(){
    m_query->admissionRejected();
    if(!m_awaitingDataAdmission)return;
    m_awaitingDataAdmission=false;if(m_recordDialog)m_recordDialog->error(tr("The operation was not submitted. Review its target and try again."));
}
void RelationalWorkbench::operationReceipt(const QJsonObject &receipt){
    m_query->operationReceipt(receipt);
    if(m_dataOperation.isEmpty() || receipt["operation_id"]!=m_dataOperation)return;
    const auto outcome=receipt["outcome"].toString();
    if(outcome=="committed"){
        m_recordDraft={};m_dataOperation.clear();if(m_recordDialog)m_recordDialog->accept();
    }else if(outcome=="rejected" || outcome=="rolled_back"){
        m_dataOperation.clear();m_recordDraft.remove("operation_id");
        if(m_recordDialog){if(receipt["code"]=="STALE_STATE")m_recordDialog->stale(receipt["message"].toString());else m_recordDialog->error(receipt["message"].toString());}
    }else if(m_recordDialog)m_recordDialog->unknown(tr("Outcome unknown. Inspect the operation before retrying. Your input is retained."));
    setLocked(m_blocked);
}

void RelationalWorkbench::dataPreview(const QJsonObject &request,const QJsonObject &reviewed,QPointer<RecordInputDialog> dialog){
    if(!dialog)return;
    if(reviewed["id"]!=m_info.value("id") || reviewed["state"]!=m_info.value("state")){dialog->stale(tr("Stored data changed. Review current values before retrying."));return;}
    m_recordDraft["request"]=request;dialog->pending(true);emit busyRequested(true);
    auto body=request;body["expected_state"]=reviewed["state"];
    m_client->request(base()+(request["action"]=="table_repair"?"/relationships/repair-preview":"/tables/edit-preview"),body,[this,reviewed,dialog](QJsonObject result,QString error){
        emit busyRequested(false);if(!dialog)return;
        if(reviewed["id"]!=m_info.value("id") || reviewed["state"]!=m_info.value("state")){dialog->stale(tr("Stored data changed. Review current values before retrying."));return;}
        if(!error.isEmpty()){dialog->error(error);return;}
        if(!result["can_apply"].toBool()){
            QStringList errors;for(const auto &value:result["issues"].toArray()){const auto issue=value.toObject();errors.append(tr("Record %1, %2: %3").arg(issue["record"].toInt()).arg(issue["column"].toString(),issue["message"].toString()));}dialog->error(errors.join('\n'));return;
        }
        auto operation=result["operation"].toObject();operation["review_digest"]=result["review_digest"];operation["workbook"]=reviewed["id"];operation["expected_state"]=reviewed["state"];
        m_awaitingDataAdmission=true;emit submitRequested(operation);
    });
}

void RelationalWorkbench::recordDialog(const QJsonObject &table,const QJsonArray &columns,const QJsonObject &stored,const QString &rowId,const QJsonObject &retained){
    const auto reviewed=m_info;RecordInputDialog dialog(columns,stored,this);m_recordDialog=&dialog;
    dialog.setWindowTitle(rowId.isEmpty()?tr("Add record to %1").arg(table["name"].toString()):tr("Edit record %1 in %2").arg(rowId,table["name"].toString()));
    dialog.explain(rowId.isEmpty()?tr("Enter typed values. NULL differs from empty Text. All values and required Python rules commit together."):
        tr("Stored record %1: %2\nYour input is applied only to these column identities. NULL differs from empty Text.").arg(rowId,QString::fromUtf8(QJsonDocument(stored).toJson(QJsonDocument::Compact)).left(2048)));
    m_recordDraft={{"path",reviewed["path"]},{"workbook_id",reviewed["workbook_id"]},{"table",table["id"]},{"columns",columns},{"row_id",rowId},{"state",reviewed["state"]}};
    if(!retained.isEmpty())dialog.setValues(retained);
    dialog.applyRequested=[this,&dialog,reviewed,table,rowId]{
        m_recordDraft["values"]=dialog.values();QJsonObject request{{"table",table["id"]}};
        if(rowId.isEmpty()){request["action"]="table_insert";request["rows"]=QJsonArray{dialog.values()};}
        else{request["action"]="table_update";request["edits"]=QJsonArray{QJsonObject{{"row_id",rowId},{"values",dialog.values()}}};}
        dataPreview(request,reviewed,&dialog);
    };
    dialog.inspectRequested=[this]{emit reconcileRequested();};dialog.preserveRequested=[this,&dialog]{m_recordDraft["values"]=dialog.values();};
    dialog.reviewRequested=[this,&dialog]{dialog.preserve();dialog.reject();QTimer::singleShot(0,this,&RelationalWorkbench::reviewRecordDraft);};
    const auto answer=dialog.exec();m_recordDialog=nullptr;
    if(answer!=QDialog::Accepted && m_dataOperation.isEmpty() && !dialog.preservesDraft())m_recordDraft={};
    setLocked(m_blocked);
}

void RelationalWorkbench::addRecord(){
    if(m_locked || !m_recordDraft.isEmpty())return;
    for(const auto &entry:m_catalog.value("tables").toArray()){const auto table=entry.toObject();if(table["id"].toString()==m_table){recordDialog(table,table["columns"].toArray(),{},{});return;}}
}
void RelationalWorkbench::editCell(int row,int column){
    if(m_locked || !m_recordDraft.isEmpty() || row<0 || column<0)return;
    const auto rows=m_page.value("records").toArray(),columns=m_page.value("columns").toArray();if(row>=rows.size() || column>=columns.size())return;
    const auto col=columns[column].toObject();const auto rowId=rows[row].toObject()["row_id"].toString();const auto reviewed=m_info;const auto target=m_table;
    emit busyRequested(true);
    m_client->request(base()+"/tables/read",{{"table",target},{"columns",QJsonArray{col["id"]}},{"record_ids",QJsonArray{rowId}},{"expected_state",reviewed["state"]}},[this,reviewed,target,col,rowId](QJsonObject result,QString error){
        emit busyRequested(false);if(!error.isEmpty()){emit issue(error);return;}if(reviewed["id"]!=m_info.value("id") || reviewed["state"]!=m_info.value("state") || target!=m_table)return;
        QJsonObject table;for(const auto &entry:m_catalog.value("tables").toArray())if(entry.toObject()["id"].toString()==target)table=entry.toObject();
        recordDialog(table,QJsonArray{col},result["records"].toArray().first().toObject()["values"].toObject(),rowId);
    });
}
void RelationalWorkbench::reviewRecordDraft(){
    if(m_locked || hasActiveEditor() || m_recordDraft.isEmpty() || !m_dataOperation.isEmpty())return;
    const auto draft=m_recordDraft;
    if(draft["path"]!=m_info.value("path") || draft["workbook_id"]!=m_info.value("workbook_id")){emit issue(tr("This draft belongs to another workbook. Copy or discard it before continuing."));return;}
    QJsonObject table;for(const auto &entry:m_catalog.value("tables").toArray())if(entry.toObject()["id"]==draft["table"])table=entry.toObject();
    if(table.isEmpty()){emit issue(tr("The original table no longer exists. The draft is retained for copying or discarding."));return;}
    if(draft.contains("bulk")){
        RecordInputDialog dialog({}, {},this);m_recordDialog=&dialog;dialog.setWindowTitle(tr("Review retained record operation"));dialog.explain(QString::fromUtf8(QJsonDocument(draft["request"].toObject()).toJson()).left(12000));
        const auto reviewed=m_info;dialog.applyRequested=[this,&dialog,draft,reviewed]{dataPreview(draft["request"].toObject(),reviewed,&dialog);};dialog.preserveRequested=[]{};dialog.preserve();dialog.exec();m_recordDialog=nullptr;setLocked(m_blocked);return;
    }
    const auto columns=draft["columns"].toArray();QJsonArray ids;
    for(const auto &col:columns){bool found=false;for(const auto &current:table["columns"].toArray())if(current.toObject()==col.toObject())found=true;if(!found){emit issue(tr("The original column definition changed. The draft cannot be retargeted automatically."));return;}ids.append(col.toObject()["id"]);}
    const auto rowId=draft["row_id"].toString();if(rowId.isEmpty()){recordDialog(table,columns,{},rowId,draft["values"].toObject());return;}
    const auto reviewed=m_info;emit busyRequested(true);
    m_client->request(base()+"/tables/read",{{"table",table["id"]},{"columns",ids},{"record_ids",QJsonArray{rowId}},{"expected_state",reviewed["state"]}},[this,reviewed,table,columns,rowId,draft](QJsonObject result,QString error){
        emit busyRequested(false);if(!error.isEmpty()){emit issue(error);return;}if(reviewed["state"]!=m_info.value("state") || reviewed["id"]!=m_info.value("id"))return;
        recordDialog(table,columns,result["records"].toArray().first().toObject()["values"].toObject(),rowId,draft["values"].toObject());
    });
}

static QList<QTableWidgetSelectionRange> ranges(QTableWidget *grid){return grid->selectedRanges();}
void RelationalWorkbench::deleteRecords(){
    if(m_locked || !m_recordDraft.isEmpty())return;QSet<int> selected;for(const auto &range:ranges(m_records))for(int r=range.topRow();r<=range.bottomRow();++r)selected.insert(r);
    if(selected.isEmpty())return;auto ordered=selected.values();std::sort(ordered.begin(),ordered.end());QJsonArray ids;
    for(int row:ordered)ids.append(m_page.value("records").toArray()[row].toObject()["row_id"]);
    m_recordDraft={{"bulk",true},{"path",m_info.value("path")},{"workbook_id",m_info.value("workbook_id")},{"table",m_table},{"request",QJsonObject{{"action","table_delete"},{"table",m_table},{"record_ids",ids}}}};reviewRecordDraft();
}
void RelationalWorkbench::clearRecordCells(){
    if(m_locked || !m_recordDraft.isEmpty())return;QMap<int,QJsonObject> values;
    for(const auto &range:ranges(m_records))for(int r=range.topRow();r<=range.bottomRow();++r)for(int c=range.leftColumn();c<=range.rightColumn();++c)values[r][m_page.value("columns").toArray()[c].toObject()["id"].toString()]=QJsonValue::Null;
    QJsonArray edits;for(auto it=values.begin();it!=values.end();++it)edits.append(QJsonObject{{"row_id",m_page.value("records").toArray()[it.key()].toObject()["row_id"]},{"values",it.value()}});if(edits.isEmpty())return;
    m_recordDraft={{"bulk",true},{"path",m_info.value("path")},{"workbook_id",m_info.value("workbook_id")},{"table",m_table},{"request",QJsonObject{{"action","table_update"},{"table",m_table},{"edits",edits}}}};reviewRecordDraft();
}
void RelationalWorkbench::copyRecordCells(){
    const auto selection=ranges(m_records);if(m_blocked || selection.size()!=1){emit issue(tr("Choose one rectangular selection to copy."));return;}
    const auto range=selection.first();QJsonArray ids,cols;for(int r=range.topRow();r<=range.bottomRow();++r)ids.append(m_page.value("records").toArray()[r].toObject()["row_id"]);for(int c=range.leftColumn();c<=range.rightColumn();++c)cols.append(m_page.value("columns").toArray()[c].toObject()["id"]);
    const auto reviewed=m_info;emit busyRequested(true);
    m_client->request(base()+"/tables/read",{{"table",m_table},{"columns",cols},{"record_ids",ids},{"expected_state",reviewed["state"]}},[this,reviewed,cols](QJsonObject result,QString error){
        emit busyRequested(false);if(!error.isEmpty()){emit issue(error);return;}if(reviewed["state"]!=m_info.value("state") || reviewed["id"]!=m_info.value("id"))return;
        QJsonArray rows;QStringList lines;for(const auto &entry:result["records"].toArray()){QJsonArray values;QStringList line;for(const auto &col:cols){const QJsonValue value=entry.toObject()["values"].toObject()[col.toString()];values.append(value);line.append(value.isNull()?QString{}:tableValueText(value));}rows.append(values);lines.append(line.join('\t'));}
        auto *mime=new QMimeData;mime->setData("application/x-datahorizon-table-values",QJsonDocument(QJsonObject{{"version",1},{"rows",rows}}).toJson(QJsonDocument::Compact));mime->setText(lines.join('\n'));QApplication::clipboard()->setMimeData(mime);
    });
}
void RelationalWorkbench::pasteRecordCells(){
    if(m_locked || !m_recordDraft.isEmpty() || m_records->currentRow()<0 || m_records->currentColumn()<0)return;
    const auto *mime=QApplication::clipboard()->mimeData();QJsonArray native;QString error;QJsonObject decoded;
    if(mime->hasFormat("application/x-datahorizon-table-values")){
        const auto bytes=mime->data("application/x-datahorizon-table-values");const auto object=QJsonDocument::fromJson(bytes).object();
        if(bytes.size()>ClipboardData::maxBytes || object["version"]!=1 || !object["rows"].isArray()){emit issue(tr("Invalid or oversized native table clipboard."));return;}native=object["rows"].toArray();
    }else if(mime->hasFormat("application/x-datahorizon-cells")){
        if(!ClipboardData::decode(mime,decoded,error) || decoded["cut"].toBool()){emit issue(error.isEmpty()?tr("Finish the sheet Cut before pasting table values."):error);return;}
        for(const auto &entry:decoded["rows"].toArray()){QJsonArray row;for(const auto &field:entry.toArray()){const auto cell=field.toObject();if(cell.contains("error") || cell["calculation"]=="pending" || cell["omitted"].toBool()){emit issue(tr("Clipboard contains unavailable values. Recalculate and copy again."));return;}row.append(cell["value"]);}native.append(row);}
    }
    const auto reviewed=m_info;const auto page=m_page;const int firstRow=m_records->currentRow(),firstColumn=m_records->currentColumn();const auto target=m_table;
    const QJsonArray fields{QJsonObject{{"id","paste"},{"name",tr("Tab-separated values")},{"type","text"},{"required",true}}};
    RecordInputDialog dialog(fields,{{"paste",mime->text()}},this,true);m_recordDialog=&dialog;dialog.setWindowTitle(tr("Paste values into existing records"));
    dialog.explain(tr("Review tab-separated values. Existing record identities and displayed columns are fixed. Blank Text stays empty; numeric blanks require explicit NULL through the typed editor. Native copies preserve NULL while this text remains unchanged."));
    m_recordDraft={{"bulk",true},{"path",reviewed["path"]},{"workbook_id",reviewed["workbook_id"]},{"table",target}};const auto original=mime->text();
    dialog.applyRequested=[this,&dialog,reviewed,page,target,firstRow,firstColumn,native,original]{
        QJsonArray rows=native;const auto text=dialog.values()["paste"].toString();if(rows.isEmpty() || text!=original){QMimeData plain;plain.setText(text);QJsonObject decoded;QString error;if(!ClipboardData::decode(&plain,decoded,error)){dialog.error(error);return;}rows={};for(const auto &entry:decoded["rows"].toArray()){QJsonArray row;for(const auto &field:entry.toArray())row.append(field.toObject()["value"]);rows.append(row);}}
        if(rows.isEmpty() || rows.size()+firstRow>page["records"].toArray().size()){dialog.error(tr("Paste must fit the loaded existing records. Add records separately."));return;}QJsonArray edits;int width=-1,count=0;
        for(int r=0;r<rows.size();++r){if(!rows[r].isArray()){dialog.error(tr("Invalid native clipboard row."));return;}const auto row=rows[r].toArray();if(width<0)width=row.size();if(row.isEmpty() || row.size()!=width || firstColumn+width>page["columns"].toArray().size() || (count+=width)>10000){dialog.error(tr("Paste must be rectangular and fit the displayed columns and 10,000-field limit."));return;}QJsonObject values;for(int c=0;c<width;++c){if(row[c].isObject() || row[c].isArray() || row[c].isUndefined()){dialog.error(tr("Clipboard contains unsupported values."));return;}values[page["columns"].toArray()[firstColumn+c].toObject()["id"].toString()]=row[c];}edits.append(QJsonObject{{"row_id",page["records"].toArray()[firstRow+r].toObject()["row_id"]},{"values",values}});}
        dataPreview({{"action","table_update"},{"table",target},{"edits",edits}},reviewed,&dialog);
    };
    dialog.inspectRequested=[this]{emit reconcileRequested();};dialog.preserveRequested=[]{};dialog.preserve();dialog.exec();m_recordDialog=nullptr;setLocked(m_blocked);
}
