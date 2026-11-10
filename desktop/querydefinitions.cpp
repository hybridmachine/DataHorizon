// SPDX-License-Identifier: GPL-3.0-only
#include "queryworkbench.h"
#include "recordinputdialog.h"
#include "relationalexport.h"
#include <QComboBox>
#include <QFormLayout>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QVBoxLayout>

bool QueryWorkbench::hasUnsavedDefinition()const{
    const auto draft=definition();return !draft["sql"].toString().isEmpty() && (m_saved.isEmpty() || draft["sql"]!=m_saved.value("sql") || draft["parameters"]!=m_saved.value("parameters"));
}
QJsonObject QueryWorkbench::recoveryDraft()const{return {{"definition",definition()},{"values",m_values},{"save_draft",m_saveDraft},{"operation_id",m_definitionOperation},{"query_job",m_job},{"path",m_info.value("path")},{"workbook_id",m_info.value("workbook_id")}};}
void QueryWorkbench::discardDefinition(){m_saved={};m_savedDigest.clear();m_saveDraft={};m_values={};setDefinition({});buttons();}
void QueryWorkbench::operationAdmitted(const QString &id){if(m_awaitingDefinition){m_awaitingDefinition=false;m_definitionOperation=id;}}
void QueryWorkbench::admissionRejected(){if(m_awaitingDefinition){m_awaitingDefinition=false;m_status->setText(tr("Definition operation was not submitted. Review the draft again."));}}
void QueryWorkbench::operationReceipt(const QJsonObject &receipt){
    if(m_definitionOperation.isEmpty() || receipt["operation_id"].toString()!=m_definitionOperation)return;
    if(receipt["outcome"]=="committed"){
        m_saved=receipt["query"].toObject();m_savedDigest=receipt["definition_digest"].toString();m_saveDraft={};m_definitionOperation.clear();m_status->setText(tr("Saved definition stored in the native workbook. Running remains explicit."));
    }else if(receipt["outcome"]=="rolled_back" || receipt["outcome"]=="rejected"){
        m_definitionOperation.clear();m_status->setText(receipt["message"].toString()+tr(" Definition draft retained; review before retrying."));
    }else m_status->setText(tr("Definition outcome unknown. Reconcile operation %1 before another write; the SQL and values remain drafts.").arg(m_definitionOperation));buttons();
}
void QueryWorkbench::loadSavedQuery(const QString &id){
    if(m_locked || m_info.isEmpty())return;
    if(hasUnsavedDefinition()){QMessageBox review(QMessageBox::Question,tr("Discard SQL draft?"),tr("Discard the current unsubmitted SQL/parameter definition and open the selected saved view?"),QMessageBox::Yes|QMessageBox::Cancel,this);review.setObjectName("discardSqlOnOpen");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setDefaultButton(QMessageBox::Cancel);if(review.exec()!=QMessageBox::Yes)return;}
    const auto info=m_info;const auto revision=m_editorRevision;m_result={};suspendView();
    m_client->request("/v1/workbooks/"+info["id"].toString()+"/queries/read",{{"query",id},{"expected_state",info["state"]}},[this,info,revision](QJsonObject result,QString error){
        if(info["id"]!=m_info.value("id") || info["state"]!=m_info.value("state") || revision!=m_editorRevision)return;if(!error.isEmpty()){emit issue(error);return;}if(result["state"]!=info["state"])return;
        setDefinition(result["query"].toObject());m_saved=result["query"].toObject();m_savedDigest=result["definition_digest"].toString();m_saveDraft={};m_status->setText(tr("Saved definition opened. No query ran; review parameters and choose Run / Refresh."));buttons();
    });
}
void QueryWorkbench::saveDefinition(bool copy){
    if(m_locked || m_info.value("readonly").toBool() || m_info.value("repair").toBool())return;const auto info=m_info;const auto revision=m_editorRevision;auto definitionDraft=definition();
    QDialog options(this);options.setObjectName("saveQueryOptions");options.setWindowTitle(copy?tr("Save query as new definition"):tr("Save query definition"));QFormLayout form(&options);QLineEdit name(m_saveDraft.value("name").toString(m_saved.value("name").toString("New query")));name.setObjectName("savedQueryNameInput");name.setMaxLength(128);QComboBox defaults;defaults.setObjectName("queryDefaultPolicy");defaults.addItems({tr("Keep existing saved defaults"),tr("Store current invocation values as defaults"),tr("Clear saved defaults")});if(copy || m_saved.isEmpty())defaults.setCurrentIndex(2);form.addRow(tr("Name"),&name);form.addRow(tr("Parameter defaults"),&defaults);QLabel note(tr("Invocation-only values are never stored automatically. The next review shows the complete proposed definition/defaults and any existing definition. Save clears session history and does not execute SQL."));note.setWordWrap(true);form.addRow(&note);QDialogButtonBox buttons(QDialogButtonBox::Ok|QDialogButtonBox::Cancel);form.addRow(&buttons);connect(&buttons,&QDialogButtonBox::accepted,&options,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&options,&QDialog::reject);
    if(options.exec()!=QDialog::Accepted)return;QJsonObject savedDefaults;const auto parameters=definitionDraft["parameters"].toArray();for(const auto &entry:parameters){const auto key=entry.toObject()["name"].toString();const auto source=defaults.currentIndex()==1?m_values:defaults.currentIndex()==0?m_saved.value("defaults").toObject():QJsonObject{};if(source.contains(key))savedDefaults[key]=source[key];}
    definitionDraft["name"]=name.text();definitionDraft["defaults"]=savedDefaults;if(!copy && !m_saved.isEmpty())definitionDraft["query"]=m_saved.value("id");definitionDraft["expected_state"]=info["state"];m_saveDraft=definitionDraft;
    if(info["state"]!=m_info.value("state") || revision!=m_editorRevision){emit issue(tr("SQL definition or stored state changed during review."));return;}
    emit busyRequested(true);m_client->request("/v1/workbooks/"+info["id"].toString()+"/queries/preview",definitionDraft,[this,info,revision](QJsonObject result,QString error){
        if(info["id"]!=m_info.value("id"))return;emit busyRequested(false);if(!error.isEmpty()){m_status->setText(error);emit issue(error);return;}if(info["state"]!=m_info.value("state") || revision!=m_editorRevision){emit issue(tr("SQL definition changed. Review it again."));return;}
        m_saveDraft=result["source_request"].toObject();QDialog review(this);review.setObjectName("queryDefinitionReview");review.setWindowTitle(tr("Review native saved query"));review.resize(760,520);QVBoxLayout layout(&review);QLabel explanation(tr("Apply stores this definition, explicit defaults, dependencies and result shape. No result rows were evaluated. Session Undo/Redo clears."));explanation.setWordWrap(true);layout.addWidget(&explanation);QPlainTextEdit contents;contents.setObjectName("queryDefinitionPreview");contents.setReadOnly(true);contents.setPlainText(QString::fromUtf8(QJsonDocument(QJsonObject{{"previous",result["previous"]},{"proposed",result["query"]}}).toJson()));layout.addWidget(&contents);QDialogButtonBox buttons(QDialogButtonBox::Apply|QDialogButtonBox::Cancel);layout.addWidget(&buttons);connect(buttons.button(QDialogButtonBox::Apply),&QPushButton::clicked,&review,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&review,&QDialog::reject);
        if(review.exec()!=QDialog::Accepted || info["id"]!=m_info.value("id") || info["state"]!=m_info.value("state") || revision!=m_editorRevision)return;
        auto operation=result["source_request"].toObject();operation["action"]="query_save";operation["review_digest"]=result["review_digest"];operation["workbook"]=info["id"];operation["expected_state"]=info["state"];m_awaitingDefinition=true;emit submitRequested(operation);
    });
}
void QueryWorkbench::deleteDefinition(){
    if(m_locked || m_saved.isEmpty())return;const auto info=m_info,saved=m_saved;const auto digest=m_savedDigest;
    QMessageBox review(QMessageBox::Question,tr("Delete saved query?"),tr("Delete %1 from the native catalog? Stored table data remains; session history clears. The current SQL stays as an unsaved draft.").arg(saved["name"].toString()),QMessageBox::Yes|QMessageBox::Cancel,this);review.setObjectName("deleteQueryReview");review.setOption(QMessageBox::Option::DontUseNativeDialog);review.setDefaultButton(QMessageBox::Cancel);if(review.exec()!=QMessageBox::Yes)return;
    m_awaitingDefinition=true;emit submitRequested({{"action","query_delete"},{"query",saved["id"]},{"definition_digest",digest},{"workbook",info["id"]},{"expected_state",info["state"]}});
}
void QueryWorkbench::exportTo(const QString &path,const QString &format){
    if(m_locked || m_result.isEmpty())return;const auto info=m_info;const auto job=m_job;const auto revision=m_editorRevision;
    emit busyRequested(true);RelationalExport::run(this,m_client,info,base()+"/"+job+"/export",{},path,format,[this,info,job,revision]{return m_info.value("id")==info["id"] && m_info.value("state")==info["state"] && m_job==job && !m_result.isEmpty() && revision==m_editorRevision;},[this](QString result){emit busyRequested(false);m_status->setText(result);});
}
