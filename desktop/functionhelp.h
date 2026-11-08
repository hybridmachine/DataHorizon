// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QDialog>
#include <QDialogButtonBox>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QSplitter>
#include <QSet>
#include <QVBoxLayout>

inline bool validFunctionCatalog(const QJsonObject &catalog) {
    if(catalog["version"].toInt()!=1 || !catalog["functions"].isArray() || catalog["functions"].toArray().isEmpty() || catalog["functions"].toArray().size()>256)return false;
    QSet<QString> names;for(const auto &value:catalog["functions"].toArray()) {const auto item=value.toObject();const auto name=item["name"].toString();if(name.isEmpty() || names.contains(name) || !item["signature"].isString() || !item["summary"].isString() || !item["rules"].isString() || !item["examples"].isArray())return false;names.insert(name);}return true;
}

class FunctionHelpDialog : public QDialog {
public:
    FunctionHelpDialog(const QJsonObject &catalog,QWidget *parent):QDialog(parent) {
        setObjectName("functionHelpDialog");setWindowTitle(tr("Implemented formula functions"));resize(760,540);auto *layout=new QVBoxLayout(this);
        auto *search=new QLineEdit(this);search->setObjectName("functionSearch");search->setPlaceholderText(tr("Search functions"));search->setAccessibleName(tr("Search implemented functions"));layout->addWidget(search);
        auto *split=new QSplitter(this);auto *list=new QListWidget(split);list->setObjectName("functionList");list->setAccessibleName(tr("Implemented functions"));auto *detail=new QPlainTextEdit(split);detail->setObjectName("functionDetails");detail->setReadOnly(true);detail->setAccessibleName(tr("Function signature, rules and examples"));split->setStretchFactor(1,2);layout->addWidget(split,1);
        for(const auto &value:catalog["functions"].toArray()){const auto function=value.toObject();auto *item=new QListWidgetItem(function["name"].toString(),list);item->setData(Qt::UserRole,function);}
        connect(search,&QLineEdit::textChanged,list,[list](const QString &text){for(int i=0;i<list->count();++i){auto *item=list->item(i);const auto f=item->data(Qt::UserRole).toJsonObject();item->setHidden(!(f["name"].toString()+" "+f["summary"].toString()).contains(text,Qt::CaseInsensitive));}for(int i=0;i<list->count();++i)if(!list->item(i)->isHidden()){list->setCurrentRow(i);return;}list->setCurrentRow(-1);});
        connect(list,&QListWidget::currentItemChanged,detail,[detail](QListWidgetItem *item){if(!item){detail->clear();return;}const auto f=item->data(Qt::UserRole).toJsonObject();QStringList lines{f["signature"].toString(),f["category"].toString(),"",f["summary"].toString(),"",f["rules"].toString(),"",tr("Examples")};for(const auto &value:f["examples"].toArray()){const auto example=value.toObject();auto result=QString::fromUtf8(QJsonDocument(QJsonArray{example["result"]}).toJson(QJsonDocument::Compact));lines.append(example["formula"].toString()+" → "+result.mid(1,result.size()-2));}detail->setPlainText(lines.join('\n'));});
        QStringList notes;for(const auto &value:catalog["notes"].toArray())notes.append(value.toString());auto *note=new QLabel(notes.join('\n'),this);note->setWordWrap(true);layout->addWidget(note);
        auto *buttons=new QDialogButtonBox(QDialogButtonBox::Close,this);connect(buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);layout->addWidget(buttons);list->setCurrentRow(0);
    }
};
