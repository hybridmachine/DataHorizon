// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QCheckBox>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QPlainTextEdit>
#include <QTableWidget>
#include <QVBoxLayout>
#include <functional>

inline QString tableValueText(const QJsonValue &value) {
    if(value.isNull())return QStringLiteral("NULL");
    if(value.isString())return value.toString();
    if(value.isBool())return value.toBool()?QStringLiteral("TRUE"):QStringLiteral("FALSE");
    const auto json=QString::fromUtf8(QJsonDocument(QJsonArray{value}).toJson(QJsonDocument::Compact));return json.mid(1,json.size()-2);
}

class RecordInputDialog : public QDialog {
public:
    std::function<void()> applyRequested,inspectRequested,reviewRequested,preserveRequested;
    explicit RecordInputDialog(const QJsonArray &columns,const QJsonObject &stored,QWidget *parent=nullptr,bool multiline=false):QDialog(parent) {
        setObjectName("recordInputDialog");resize(620,400);auto *layout=new QVBoxLayout(this);
        m_explanation=new QLabel; m_explanation->setWordWrap(true);m_explanation->setObjectName("recordStoredValues");layout->addWidget(m_explanation);
        auto *fields=new QTableWidget(columns.size(),3);fields->setObjectName("recordInputs");fields->setAccessibleName(tr("Typed record values"));fields->setHorizontalHeaderLabels({tr("Column and type"),tr("Value"),tr("NULL")});
        fields->horizontalHeader()->setSectionResizeMode(0,QHeaderView::ResizeToContents);fields->horizontalHeader()->setSectionResizeMode(1,QHeaderView::Stretch);layout->addWidget(fields,1);
        for(int row=0;row<columns.size();++row){
            const auto column=columns[row].toObject();const auto value=stored.value(column["id"].toString());Editor entry;entry.column=column;
            auto *label=new QTableWidgetItem(column["name"].toString()+" ("+column["type"].toString()+")");label->setFlags(label->flags()&~Qt::ItemIsEditable);fields->setItem(row,0,label);
            if(multiline){entry.text=new QPlainTextEdit(value.toString());entry.text->setObjectName("recordPasteText");entry.text->setAccessibleName(tr("Tab-separated values"));fields->setCellWidget(row,1,entry.text);fields->setRowHeight(row,180);}
            else if(column["type"]=="boolean") {entry.combo=new QComboBox;entry.combo->addItem(tr("TRUE"),true);entry.combo->addItem(tr("FALSE"),false);entry.combo->setCurrentIndex(value.toBool()?0:1);entry.combo->setObjectName("recordInput_"+QString::number(row));entry.combo->setAccessibleName(column["name"].toString());fields->setCellWidget(row,1,entry.combo);}
            else {entry.line=new QLineEdit(value.isNull() || value.isUndefined()?QString{}:tableValueText(value));entry.line->setObjectName("recordInput_"+QString::number(row));entry.line->setAccessibleName(column["name"].toString());entry.line->setMaxLength(65536);fields->setCellWidget(row,1,entry.line);}
            entry.null=new QCheckBox;entry.null->setObjectName("recordNull_"+QString::number(row));entry.null->setAccessibleName(tr("%1 is NULL").arg(column["name"].toString()));entry.null->setChecked(!column["required"].toBool() && (value.isUndefined() || value.isNull()));entry.null->setEnabled(!column["required"].toBool());entry.null->setToolTip(tr("Required means non-NULL. Empty Text is a separate value."));fields->setCellWidget(row,2,entry.null);
            auto update=[line=entry.line,combo=entry.combo,box=entry.null]{if(line)line->setEnabled(!box->isChecked());if(combo)combo->setEnabled(!box->isChecked());};connect(entry.null,&QCheckBox::toggled,this,update);update();m_editors.append(entry);
        }
        m_status=new QLabel;m_status->setObjectName("recordEditStatus");m_status->setWordWrap(true);m_status->setTextInteractionFlags(Qt::TextSelectableByMouse);layout->addWidget(m_status);
        auto *actions=new QHBoxLayout;m_inspect=new QPushButton(tr("Inspect operation status"));m_inspect->setObjectName("inspectRecordOperation");m_inspect->hide();m_review=new QPushButton(tr("Review current stored values"));m_review->setObjectName("reviewRecordDraft");m_review->hide();actions->addWidget(m_inspect);actions->addWidget(m_review);actions->addStretch();layout->addLayout(actions);
        m_buttons=new QDialogButtonBox(QDialogButtonBox::Save|QDialogButtonBox::Cancel);layout->addWidget(m_buttons);
        connect(m_buttons->button(QDialogButtonBox::Save),&QPushButton::clicked,this,[this]{if(applyRequested)applyRequested();});connect(m_buttons,&QDialogButtonBox::rejected,this,&RecordInputDialog::reject);
        connect(m_inspect,&QPushButton::clicked,this,[this]{if(inspectRequested)inspectRequested();});connect(m_review,&QPushButton::clicked,this,[this]{if(reviewRequested)reviewRequested();});
    }
    QJsonObject values()const {
        QJsonObject result;for(const auto &entry:m_editors)result[entry.column["id"].toString()]=entry.null->isChecked()?QJsonValue(QJsonValue::Null):entry.combo?QJsonValue(entry.combo->currentData().toBool()):QJsonValue(entry.text?entry.text->toPlainText():entry.line->text());return result;
    }
    void setValues(const QJsonObject &values) {
        for(const auto &entry:m_editors){const auto id=entry.column["id"].toString();if(!values.contains(id))continue;const auto value=values[id];entry.null->setChecked(value.isNull());if(entry.line)entry.line->setText(value.isNull()?QString{}:tableValueText(value));if(entry.text)entry.text->setPlainText(value.toString());if(entry.combo)entry.combo->setCurrentIndex(value.toBool()?0:1);}
    }
    void explain(const QString &text){m_explanation->setText(text);}
    void pending(bool value) {
        m_pending=value;for(const auto &entry:m_editors){if(entry.line)entry.line->setEnabled(!value && !entry.null->isChecked());if(entry.text)entry.text->setEnabled(!value);if(entry.combo)entry.combo->setEnabled(!value && !entry.null->isChecked());entry.null->setEnabled(!value && !entry.column["required"].toBool());}
        m_buttons->button(QDialogButtonBox::Save)->setEnabled(!value);m_buttons->button(QDialogButtonBox::Cancel)->setEnabled(!value);
        if(value)m_status->setText(tr("Submitting reviewed record values…"));
    }
    void error(const QString &text){m_unknown=false;m_inspect->hide();pending(false);m_status->setText(text);}
    void unknown(const QString &text){pending(true);m_preserve=true;m_unknown=true;m_status->setText(text);m_inspect->show();m_buttons->button(QDialogButtonBox::Cancel)->setEnabled(true);}
    void stale(const QString &text){error(text);m_preserve=true;m_review->show();m_buttons->button(QDialogButtonBox::Save)->setEnabled(false);}
    void preserve(){m_preserve=true;m_buttons->button(QDialogButtonBox::Cancel)->setText(tr("Keep draft and close"));}
    bool preservesDraft()const{return m_preserve;}
    void reject()override {if(m_pending && !m_unknown)return;if(m_preserve && preserveRequested)preserveRequested();QDialog::reject();}
private:
    struct Editor{QJsonObject column;QLineEdit *line=nullptr;QPlainTextEdit *text=nullptr;QComboBox *combo=nullptr;QCheckBox *null=nullptr;};
    QList<Editor> m_editors;QLabel *m_status,*m_explanation;QDialogButtonBox *m_buttons;QPushButton *m_inspect,*m_review;
    bool m_pending=false,m_unknown=false,m_preserve=false;
};
