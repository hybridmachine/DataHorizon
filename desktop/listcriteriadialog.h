// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QCheckBox>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QSpinBox>
#include <QTableWidget>
#include <QVBoxLayout>
#include <cmath>

class ListCriteriaDialog : public QDialog {
public:
    ListCriteriaDialog(QJsonObject scope,QWidget *parent=nullptr):QDialog(parent),m_scope(scope) {
        setObjectName("listCriteriaDialog");setWindowTitle(tr("Sort and filter source rectangle"));resize(800,620);
        auto *layout=new QVBoxLayout(this);
        auto *note=new QLabel(tr("Source rectangle: rows %1–%2, columns %3–%4. Sort priority is top to bottom. All filters must match. No sort keys keeps source order. Original source rows remain stable.")
            .arg(scope["r1"].toInt()).arg(scope["r2"].toInt()).arg(scope["c1"].toInt()).arg(scope["c2"].toInt()),this);note->setWordWrap(true);layout->addWidget(note);
        m_header=new QCheckBox(tr("First row is a header"),this);m_header->setObjectName("listHeader");m_header->setChecked(scope["header"].toBool(false));layout->addWidget(m_header);
        m_sort=table(layout,true);m_filters=table(layout,false);
        m_error=new QLabel(this);m_error->setObjectName("criteriaError");m_error->setWordWrap(true);m_error->setAccessibleName(tr("Criteria validation error"));layout->addWidget(m_error);
        auto *buttons=new QDialogButtonBox(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,this);layout->addWidget(buttons);
        connect(buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);
        connect(buttons,&QDialogButtonBox::accepted,this,[this]{if(read())accept();});
        for(const auto &value:scope["sort"].toArray())add(m_sort,true,value.toObject());
        for(const auto &value:scope["filters"].toArray())add(m_filters,false,value.toObject());
    }
    QJsonObject scope()const{return m_scope;}
private:
    QTableWidget *table(QVBoxLayout *layout,bool sort) {
        auto *label=new QLabel(sort?tr("Sort keys (maximum 8)"):tr("Filters (AND, maximum 8)"),this);layout->addWidget(label);
        auto *view=new QTableWidget(0,sort?2:4,this);view->setObjectName(sort?"sortCriteria":"filterCriteria");view->setAccessibleName(label->text());
        view->setHorizontalHeaderLabels(sort?QStringList{tr("Source column (A=1)"),tr("Direction")}:QStringList{tr("Source column (A=1)"),tr("Comparison"),tr("Value type"),tr("Value")});
        view->setColumnWidth(0,160);view->horizontalHeader()->setStretchLastSection(true);view->setSelectionBehavior(QAbstractItemView::SelectRows);view->setSelectionMode(QAbstractItemView::SingleSelection);
        view->setMinimumHeight(130);layout->addWidget(view,1);
        auto *row=new QHBoxLayout;layout->addLayout(row);
        auto button=[&](const QString &name,const QString &id,auto callback){auto *b=new QPushButton(name,this);b->setObjectName(id);b->setAccessibleName((sort?tr("Sort keys: "):tr("Filters: "))+name);row->addWidget(b);connect(b,&QPushButton::clicked,this,callback);return b;};
        auto *addButton=button(tr("Add"),sort?"addSort":"addFilter",[this,view,sort]{add(view,sort,{});});
        connect(view->model(),&QAbstractItemModel::rowsInserted,addButton,[view,addButton]{addButton->setEnabled(view->rowCount()<8);});
        connect(view->model(),&QAbstractItemModel::rowsRemoved,addButton,[view,addButton]{addButton->setEnabled(view->rowCount()<8);});
        button(tr("Remove"),sort?"removeSort":"removeFilter",[this,view,sort]{const int row=view->currentRow();if(row>=0)view->removeRow(row);labelRows(view,sort);});
        button(tr("Move up"),sort?"sortUp":"filterUp",[this,view]{move(view,-1);});
        button(tr("Move down"),sort?"sortDown":"filterDown",[this,view]{move(view,1);});row->addStretch();return view;
    }
    void add(QTableWidget *view,bool sort,QJsonObject criterion) {
        if(view->rowCount()>=8)return;const int row=view->rowCount();view->insertRow(row);
        auto *column=new QSpinBox(view);column->setRange(m_scope["c1"].toInt(),m_scope["c2"].toInt());column->setValue(criterion.value("col").toInt(m_scope["c1"].toInt()));view->setCellWidget(row,0,column);
        auto *choice=new QComboBox(view);choice->addItems(sort?QStringList{tr("Ascending"),tr("Descending")}:QStringList{"=","<>",">","<",">=","<=","contains"});view->setCellWidget(row,1,choice);
        if(sort)choice->setCurrentIndex(criterion.value("descending").toBool()?1:0);
        else {
            choice->setCurrentText(criterion.value("op").toString("="));auto *type=new QComboBox(view);type->addItems({tr("Text"),tr("Number"),tr("Boolean"),tr("NULL")});view->setCellWidget(row,2,type);
            auto *value=new QLineEdit(view);value->setMaxLength(65536);view->setCellWidget(row,3,value);
            auto item=criterion.value("value");if(!criterion.isEmpty() && item.isUndefined())item=QJsonValue::Null;
            if(item.isDouble()){type->setCurrentIndex(1);value->setText(QString::number(item.toDouble(),'g',17));}
            else if(item.isBool()){type->setCurrentIndex(2);value->setText(item.toBool()?"TRUE":"FALSE");}
            else if(item.isNull()){type->setCurrentIndex(3);value->setEnabled(false);}
            else value->setText(item.toString());
            connect(type,&QComboBox::currentIndexChanged,value,[value](int index){value->setEnabled(index!=3);});
        }
        view->setCurrentCell(row,0);labelRows(view,sort);
    }
    void labelRows(QTableWidget *view,bool sort) {
        const QStringList names=sort?QStringList{tr("source column"),tr("direction")}:QStringList{tr("source column"),tr("comparison"),tr("value type"),tr("value")};
        for(int row=0;row<view->rowCount();++row)for(int col=0;col<view->columnCount();++col)
            if(auto *widget=view->cellWidget(row,col))widget->setAccessibleName((sort?tr("Sort key %1 "):tr("Filter %1 ")).arg(row+1)+names[col]);
    }
    void move(QTableWidget *view,int direction) {
        const int row=view->currentRow(),next=row+direction;if(row<0 || next<0 || next>=view->rowCount())return;
        for(int col=0;col<view->columnCount();++col) {
            if(auto *a=qobject_cast<QSpinBox *>(view->cellWidget(row,col))){auto *b=qobject_cast<QSpinBox *>(view->cellWidget(next,col));const int value=a->value();a->setValue(b->value());b->setValue(value);}
            else if(auto *a=qobject_cast<QComboBox *>(view->cellWidget(row,col))){auto *b=qobject_cast<QComboBox *>(view->cellWidget(next,col));const int value=a->currentIndex();a->setCurrentIndex(b->currentIndex());b->setCurrentIndex(value);}
            else if(auto *a=qobject_cast<QLineEdit *>(view->cellWidget(row,col))){auto *b=qobject_cast<QLineEdit *>(view->cellWidget(next,col));const auto value=a->text();a->setText(b->text());b->setText(value);}
        }
        view->setCurrentCell(next,0);
    }
    bool read() {
        QJsonArray sort,filters;
        for(int row=0;row<m_sort->rowCount();++row)sort.append(QJsonObject{{"col",qobject_cast<QSpinBox *>(m_sort->cellWidget(row,0))->value()},{"descending",qobject_cast<QComboBox *>(m_sort->cellWidget(row,1))->currentIndex()==1}});
        for(int row=0;row<m_filters->rowCount();++row) {
            const int col=qobject_cast<QSpinBox *>(m_filters->cellWidget(row,0))->value(),type=qobject_cast<QComboBox *>(m_filters->cellWidget(row,2))->currentIndex();
            const auto op=qobject_cast<QComboBox *>(m_filters->cellWidget(row,1))->currentText();
            const auto text=qobject_cast<QLineEdit *>(m_filters->cellWidget(row,3))->text();QJsonValue value=text;QString error;
            if(text.toUtf8().size()>65536)error=tr("Filter %1 exceeds 64 KiB.").arg(row+1);
            else if(type==1){const auto input=text.trimmed();bool ok=false;const double number=input.toDouble(&ok);
                if(!QRegularExpression("^[+-]?(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?$").match(input).hasMatch() || !ok || !std::isfinite(number))error=tr("Filter %1 needs a finite invariant decimal number.").arg(row+1);else value=number;}
            else if(type==2){if(text.trimmed().compare("TRUE",Qt::CaseInsensitive)==0)value=true;else if(text.trimmed().compare("FALSE",Qt::CaseInsensitive)==0)value=false;else error=tr("Filter %1 Boolean must be TRUE or FALSE.").arg(row+1);}
            else if(type==3)value=QJsonValue::Null;
            if(error.isEmpty() && op=="contains" && type!=0)error=tr("Filter %1: contains requires Text.").arg(row+1);
            if(!error.isEmpty()){m_error->setText(error);return false;}
            filters.append(QJsonObject{{"col",col},{"op",op},{"value",value}});
        }
        m_scope["header"]=m_header->isChecked();m_scope["sort"]=sort;m_scope["filters"]=filters;return true;
    }
    QJsonObject m_scope;QCheckBox *m_header=nullptr;QTableWidget *m_sort=nullptr,*m_filters=nullptr;QLabel *m_error=nullptr;
};
