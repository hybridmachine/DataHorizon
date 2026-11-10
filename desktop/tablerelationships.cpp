// SPDX-License-Identifier: GPL-3.0-only
#include "relationalworkbench.h"
#include "recordinputdialog.h"
#include <QComboBox>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMessageBox>
#include <QPushButton>
#include <QTableWidget>
#include <QVBoxLayout>

void RelationalWorkbench::manageRelationships(){
    if(m_blocked)return;const auto reviewed=m_info;const auto tables=m_catalog["tables"].toArray();
    QDialog dialog(this);dialog.setWindowTitle(tr("Restrictive relationships"));dialog.setObjectName("relationshipsDialog");dialog.resize(700,420);QVBoxLayout layout(&dialog);
    QLabel note(tr("Same-type child keys reference a required UNIQUE parent column using stored equality. Updates/deletes restrict; NULL children are allowed. Self references and cycles are outside this tier. Parent rebuilds require removing incoming relationships first."));note.setWordWrap(true);layout.addWidget(&note);QListWidget list;layout.addWidget(&list);
    auto describe=[tables](const QJsonValue &tid,const QJsonValue &cid){for(const auto &entry:tables){const auto table=entry.toObject();if(table["id"]!=tid)continue;for(const auto &col:table["columns"].toArray())if(col.toObject()["id"]==cid)return table["name"].toString()+"."+col.toObject()["name"].toString();}return QString{};};
    for(const auto &entry:m_catalog["relationships"].toArray()){const auto rel=entry.toObject();auto *item=new QListWidgetItem(rel["name"].toString()+": "+describe(rel["child_table"],rel["child_column"])+" → "+describe(rel["parent_table"],rel["parent_column"])+" (RESTRICT)",&list);item->setData(Qt::UserRole,rel["id"]);}
    QPushButton add(tr("Create relationship…")),drop(tr("Remove selected relationship…"));add.setObjectName("createRelationship");drop.setObjectName("dropRelationship");add.setEnabled(!m_locked);drop.setEnabled(!m_locked);layout.addWidget(&add);layout.addWidget(&drop);QDialogButtonBox close(QDialogButtonBox::Close);layout.addWidget(&close);connect(&close,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
    connect(&drop,&QPushButton::clicked,&dialog,[this,&dialog,&list,reviewed]{if(!list.currentItem())return;const auto id=list.currentItem()->data(Qt::UserRole).toJsonValue();if(QMessageBox::question(&dialog,tr("Remove relationship?"),tr("Remove the constraint and its owned index, retaining records? Session history clears."))!=QMessageBox::Yes)return;dialog.accept();emit submitRequested({{"action","relationship_drop"},{"relationship",id},{"workbook",reviewed["id"]},{"expected_state",reviewed["state"]}});});
    connect(&add,&QPushButton::clicked,&dialog,[this,&dialog,tables,reviewed]{
        QDialog create(&dialog);create.setObjectName("createRelationshipDialog");create.setWindowTitle(tr("Review native relationship"));QFormLayout form(&create);QLineEdit name;name.setObjectName("relationshipName");QComboBox parent,child;parent.setObjectName("relationshipParent");child.setObjectName("relationshipChild");
        for(const auto &entry:tables){const auto table=entry.toObject();for(const auto &value:table["columns"].toArray()){const auto col=value.toObject();const auto label=table["name"].toString()+"."+col["name"].toString()+" ("+col["type"].toString()+")";const QJsonObject key{{"table",table["id"]},{"column",col["id"]}};child.addItem(label,key);if(col["required"].toBool() && col["unique"].toBool())parent.addItem(label,key);}}
        form.addRow(tr("Name"),&name);form.addRow(tr("Parent required UNIQUE key"),&parent);form.addRow(tr("Child key (same type)"),&child);QLabel review(tr("Apply atomically rebuilds the child table and adds a supporting index. Existing orphans, dependencies, limits and required rules can reject it. Session Undo/Redo clears."));review.setWordWrap(true);form.addRow(&review);QDialogButtonBox buttons(QDialogButtonBox::Apply|QDialogButtonBox::Cancel);form.addRow(&buttons);buttons.button(QDialogButtonBox::Apply)->setEnabled(parent.count()>0 && child.count()>0);connect(buttons.button(QDialogButtonBox::Apply),&QPushButton::clicked,&create,&QDialog::accept);connect(&buttons,&QDialogButtonBox::rejected,&create,&QDialog::reject);
        if(create.exec()!=QDialog::Accepted)return;const auto p=parent.currentData().toJsonObject(),c=child.currentData().toJsonObject();dialog.accept();emit submitRequested({{"action","relationship_create"},{"name",name.text()},{"parent_table",p["table"]},{"parent_column",p["column"]},{"child_table",c["table"]},{"child_column",c["column"]},{"workbook",reviewed["id"]},{"expected_state",reviewed["state"]}});
    });dialog.exec();
}

void RelationalWorkbench::inspectViolations(){
    if(m_blocked)return;const auto reviewed=m_info;emit busyRequested(true);
    m_client->request(base()+"/relationships/inspect",{{"expected_state",reviewed["state"]}},[this,reviewed](QJsonObject report,QString error){
        emit busyRequested(false);if(!error.isEmpty()){emit issue(error);return;}if(reviewed["id"]!=m_info["id"] || reviewed["state"]!=m_info["state"])return;
        QDialog dialog(this);dialog.setObjectName("relationshipViolationInspector");dialog.setWindowTitle(tr("Stored relationship violations"));dialog.resize(720,420);QVBoxLayout layout(&dialog);
        QLabel note(report["repair"].toBool()?tr("Restricted repair handle. Review every orphan key or explicitly delete orphan records. All relationship orphans must be resolved after required rules before commit. Reopen normally when finished."):tr("Inspection does not replay skipped handlers. Use File → Open for relationship repair to admit reviewed orphan fixes."));note.setWordWrap(true);layout.addWidget(&note);
        const auto rows=report["orphans"].toArray(),relations=report["relationships"].toArray();QTableWidget grid(rows.size(),3);grid.setObjectName("relationshipOrphans");grid.setAccessibleName(tr("Orphan child keys"));grid.setHorizontalHeaderLabels({tr("Relationship"),tr("Record ID"),tr("Child key preview")});grid.setEditTriggers(QAbstractItemView::NoEditTriggers);grid.setSelectionBehavior(QAbstractItemView::SelectRows);grid.setSelectionMode(QAbstractItemView::SingleSelection);
        for(int r=0;r<rows.size();++r){const auto row=rows[r].toObject();QString name;for(const auto &rel:relations)if(rel.toObject()["id"]==row["relationship"])name=rel.toObject()["name"].toString();grid.setItem(r,0,new QTableWidgetItem(name));grid.setItem(r,1,new QTableWidgetItem(row["row_id"].toString()));grid.setItem(r,2,new QTableWidgetItem(row["key_preview"].toString()+(row["key_omitted"].toBool()?"…":"")));}layout.addWidget(&grid,1);
        QLabel status(tr("%1 orphan previews%2. Semantic issues: %3.").arg(rows.size()).arg(report["has_more"].toBool()?tr(" (more exist; use managed CLI/SDK or independent repair)"):QString{}).arg(report["semantic_issues"].toArray().size()));status.setWordWrap(true);layout.addWidget(&status);
        QPushButton update(tr("Review keys for selected relationship…")),remove(tr("Delete its orphan records…"));update.setObjectName("repairRelationshipKeys");remove.setObjectName("deleteRelationshipOrphans");const bool allowed=report["repair"].toBool() && !reviewed["readonly"].toBool() && !report["has_more"].toBool() && m_recordDraft.isEmpty();update.setEnabled(allowed);remove.setEnabled(allowed);layout.addWidget(&update);layout.addWidget(&remove);QDialogButtonBox close(QDialogButtonBox::Close);layout.addWidget(&close);connect(&close,&QDialogButtonBox::rejected,&dialog,&QDialog::reject);
        auto repair=[this,&dialog,&grid,rows,relations,reviewed](bool deleting){
            if(grid.currentRow()<0)return;const auto relId=rows[grid.currentRow()].toObject()["relationship"];QJsonObject relation;for(const auto &entry:relations)if(entry.toObject()["id"]==relId)relation=entry.toObject();QJsonArray fields,ids;QJsonObject stored;QString childColumn;
            for(const auto &entry:m_catalog["tables"].toArray())if(entry.toObject()["id"]==relation["child_table"])for(const auto &col:entry.toObject()["columns"].toArray())if(col.toObject()["id"]==relation["child_column"])childColumn=col.toObject()["id"].toString();
            if(childColumn.isEmpty()){emit issue(tr("Refresh the table catalog before reviewing repair values."));return;}
            for(const auto &entry:rows){const auto row=entry.toObject();if(row["relationship"]!=relId)continue;ids.append(row["row_id"]);if(!deleting){QJsonObject col;for(const auto &table:m_catalog["tables"].toArray())for(const auto &column:table.toObject()["columns"].toArray())if(column.toObject()["id"]==relation["child_column"])col=column.toObject();col["id"]=row["row_id"];col["name"]=tr("Record %1 — replacement key").arg(row["row_id"].toString());fields.append(col);stored[row["row_id"].toString()]=QJsonValue::Null;}}
            dialog.accept();RecordInputDialog editor(fields,stored,this);m_recordDialog=&editor;editor.setWindowTitle(deleting?tr("Review deletion of orphan records"):tr("Review replacement child keys"));editor.explain(tr("Relationship %1; record IDs %2. All orphan relationships must be valid after required rules. Save commits this reviewed repair atomically and clears session history.").arg(relation["name"].toString(),QString::fromUtf8(QJsonDocument(ids).toJson(QJsonDocument::Compact))));
            m_recordDraft={{"bulk",true},{"path",reviewed["path"]},{"workbook_id",reviewed["workbook_id"]},{"table",relation["child_table"]}};
            auto request=[&editor,deleting,ids,relId,childColumn]{QJsonObject request{{"action","table_repair"},{"relationship",relId},{"mode",deleting?"delete":"update"}};if(deleting)request["record_ids"]=ids;else{QJsonArray edits;const auto values=editor.values();for(const auto &id:ids)edits.append(QJsonObject{{"row_id",id},{"values",QJsonObject{{childColumn,values[id.toString()]}}}});request["edits"]=edits;}return request;};
            editor.applyRequested=[this,&editor,request,reviewed]{dataPreview(request(),reviewed,&editor);};editor.inspectRequested=[this]{emit reconcileRequested();};editor.preserveRequested=[this,request]{m_recordDraft["request"]=request();};const auto answer=editor.exec();m_recordDialog=nullptr;if(answer!=QDialog::Accepted && !editor.preservesDraft() && m_dataOperation.isEmpty())m_recordDraft={};setLocked(m_blocked);
        };connect(&update,&QPushButton::clicked,&dialog,[repair]{repair(false);});connect(&remove,&QPushButton::clicked,&dialog,[repair]{repair(true);});dialog.exec();
    });
}
