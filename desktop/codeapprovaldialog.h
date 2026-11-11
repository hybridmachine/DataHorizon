// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QApplication>
#include <QClipboard>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFontDatabase>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QMap>
#include <QPushButton>
#include <QVBoxLayout>
#include <optional>

// Review & approve for one catalog snapshot. It only displays: the caller
// builds the approval key from the same snapshot it passed in, so the dialog
// can never approve anything other than what it showed. For a service this
// window did not start there is no approve button; the operator's start-up
// option and the activation key are shown instead.
class CodeApprovalDialog : public QDialog {
public:
    CodeApprovalDialog(const QString &fileName,const QJsonObject &catalog,const std::optional<QJsonObject> &lastApproved,bool owned,QWidget *parent=nullptr):QDialog(parent) {
        setObjectName("codeApprovalDialog");setWindowTitle(tr("Approve Python code for %1?").arg(fileName));resize(620,440);
        const auto mono=QFontDatabase::systemFont(QFontDatabase::FixedFont);
        auto *layout=new QVBoxLayout(this);
        auto *form=new QFormLayout;layout->addLayout(form);
        auto *changes=new QLabel(describeChanges(catalog,lastApproved));changes->setObjectName("approvalChanges");changes->setWordWrap(true);
        changes->setTextInteractionFlags(Qt::TextSelectableByMouse);form->addRow(tr("Changed since last approval"),changes);
        auto *fingerprint=new QLabel(catalog["fingerprint"].toString());fingerprint->setObjectName("approvalFingerprint");fingerprint->setFont(mono);
        fingerprint->setWordWrap(true);fingerprint->setTextInteractionFlags(Qt::TextSelectableByMouse|Qt::TextSelectableByKeyboard);fingerprint->setAccessibleName(tr("Code fingerprint"));
        form->addRow(tr("Code fingerprint"),fingerprint);

        QStringList bindings;
        for(const auto &value:catalog["bindings"].toArray()) {
            const auto binding=value.toObject();const auto scope=binding["scope"].toObject();
            QString where=binding["reference"].toString();
            if(binding["broken"].toBool())where=tr("broken scope");
            else if(where.isEmpty())where=scope["kind"].toString()=="table"?tr("table"):tr("whole sheet");
            auto line=tr("%1.%2 on %3 · %4").arg(binding["module"].toString(),binding["function"].toString(),binding["event"].toString(),where);
            if(binding["required"].toBool())line+=tr(" (required)");
            bindings.append(line);
        }
        if(!bindings.isEmpty()) {
            auto *heading=new QLabel(tr("Runs automatically on edits"));heading->setObjectName("approvalBindingsHeading");
            auto font=heading->font();font.setBold(true);heading->setFont(font);layout->addWidget(heading);
            auto *list=new QLabel(bindings.join('\n'));list->setObjectName("approvalBindings");list->setWordWrap(true);list->setTextInteractionFlags(Qt::TextSelectableByMouse);
            list->setAccessibleName(tr("Runs automatically on edits"));layout->addWidget(list);
        }

        auto *warning=new QLabel(tr("Approved code runs with your own account's access to files and the network. It is not sandboxed, and undo cannot reverse effects outside the workbook."));
        warning->setObjectName("approvalWarning");warning->setWordWrap(true);layout->addWidget(warning);

        const bool approved=catalog["approved"].toBool();
        const auto key=catalog["activation_key"].toString();
        QString notice;
        if(!owned)notice=tr("This service was started separately. Its operator approves code by starting it with --approve-code %1.").arg(key);
        else if(approved)notice=tr("This code is already approved for this session.");
        if(!notice.isEmpty()) {
            auto *label=new QLabel(notice);label->setObjectName("approvalNotice");label->setWordWrap(true);label->setTextInteractionFlags(Qt::TextSelectableByMouse);layout->addWidget(label);
        }
        if(!owned) {
            auto *row=new QHBoxLayout;layout->addLayout(row);
            auto *field=new QLineEdit(key);field->setObjectName("activationKeyField");field->setReadOnly(true);field->setFont(mono);field->setAccessibleName(tr("Activation key"));row->addWidget(field,1);
            auto *copy=new QPushButton(tr("Copy"));copy->setObjectName("copyActivationKeyButton");copy->setAutoDefault(false);row->addWidget(copy);
            connect(copy,&QPushButton::clicked,this,[key]{QApplication::clipboard()->setText(key);});
        }
        layout->addStretch(1);

        auto *buttons=new QDialogButtonBox;layout->addWidget(buttons);
        auto *cancel=buttons->addButton(QDialogButtonBox::Cancel);cancel->setObjectName("cancelButton");
        if(!owned)cancel->setText(tr("Close"));
        connect(cancel,&QPushButton::clicked,this,&QDialog::reject);
        if(owned) {
            auto *approve=buttons->addButton(tr("Approve for this session"),QDialogButtonBox::AcceptRole);approve->setObjectName("approveButton");
            approve->setEnabled(!approved);approve->setAutoDefault(false);
            connect(approve,&QPushButton::clicked,this,&QDialog::accept);
        }
        // Approval is never the default action.
        cancel->setDefault(true);cancel->setFocus();
    }

    // Module names new, changed or removed since the last snapshot approved in
    // this session, and a line when the event bindings changed.
    static QString describeChanges(const QJsonObject &catalog,const std::optional<QJsonObject> &lastApproved) {
        QStringList lines;
        QMap<QString,QJsonObject> before,now;
        for(const auto &value:catalog["modules"].toArray())now[value.toObject()["id"].toString()]=value.toObject();
        if(lastApproved)for(const auto &value:(*lastApproved)["modules"].toArray())before[value.toObject()["id"].toString()]=value.toObject();
        auto module=[](const QJsonObject &value){return QJsonObject{{"name",value["name"]},{"source",value["source"]},{"macros",value["macros"]}};};
        for(auto it=now.cbegin();it!=now.cend();++it) {
            const auto name=it.value()["name"].toString();
            if(!before.contains(it.key()))lines.append(tr("%1 (new)").arg(name));
            else if(module(before[it.key()])!=module(it.value()))lines.append(tr("%1 (changed)").arg(name));
        }
        for(auto it=before.cbegin();it!=before.cend();++it)if(!now.contains(it.key()))lines.append(tr("%1 (removed)").arg(it.value()["name"].toString()));
        if(lastApproved && (*lastApproved)["bindings"].toArray()!=catalog["bindings"].toArray())lines.append(tr("Event bindings changed"));
        else if(!lastApproved && !catalog["bindings"].toArray().isEmpty())lines.append(tr("Event bindings (new)"));
        if(!lastApproved)lines.append(tr("Nothing has been approved in this session."));
        else if(lines.isEmpty())lines.append(tr("No changes since the last approval."));
        return lines.join('\n');
    }
};
