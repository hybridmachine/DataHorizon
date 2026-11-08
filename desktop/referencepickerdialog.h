// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "sheetmodel.h"
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QTableView>
#include <QVBoxLayout>
#include <QJsonArray>

class ReferencePickerDialog : public QDialog {
public:
    ReferencePickerDialog(RestClient *client,const QString &workbook,const QString &state,const QJsonArray &sheets,const QString &origin,QWidget *parent):QDialog(parent) {
        setObjectName("referencePickerDialog");setWindowTitle(tr("Pick a reference from another sheet"));resize(800,520);auto *layout=new QVBoxLayout(this);
        auto *help=new QLabel(tr("Choose a sheet, then click or drag cells. Your original formula cell stays selected. Enter an address to reach a distant range."),this);help->setWordWrap(true);layout->addWidget(help);
        m_sheet=new QComboBox(this);m_sheet->setObjectName("referenceSheet");m_sheet->setAccessibleName(tr("Reference sheet"));for(const auto &value:sheets){const auto sheet=value.toObject();if(sheet["id"].toString()!=origin)m_sheet->addItem(sheet["name"].toString(),sheet["id"].toString());}layout->addWidget(m_sheet);
        m_address=new QLineEdit(this);m_address->setObjectName("referenceAddress");m_address->setAccessibleName(tr("Reference address or range"));m_address->setPlaceholderText("A1 or A1:B10 • Enter to select");m_address->setMaxLength(64);layout->addWidget(m_address);
        m_model=new SheetModel(client,this);m_grid=new QTableView(this);m_grid->setObjectName("referencePickerGrid");m_grid->setAccessibleName(tr("Read-only reference preview"));m_grid->setModel(m_model);m_grid->setEditTriggers(QAbstractItemView::NoEditTriggers);m_grid->setSelectionMode(QAbstractItemView::ContiguousSelection);m_grid->horizontalHeader()->setDefaultSectionSize(110);m_grid->verticalHeader()->setDefaultSectionSize(26);layout->addWidget(m_grid);
        m_status=new QLabel(this);m_status->setObjectName("referencePickerStatus");m_status->setWordWrap(true);layout->addWidget(m_status);
        auto *buttons=new QDialogButtonBox(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,this);layout->addWidget(buttons);m_insert=buttons->button(QDialogButtonBox::Ok);m_insert->setText(tr("Insert reference"));for(auto *button:buttons->buttons())if(auto *push=qobject_cast<QPushButton *>(button)){push->setAutoDefault(false);push->setDefault(false);}
        connect(buttons,&QDialogButtonBox::accepted,this,[this]{if(!range().isEmpty() && !m_expired)accept();});connect(buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);
        connect(m_grid->selectionModel(),&QItemSelectionModel::selectionChanged,this,[this]{m_addressPending=false;m_address->setModified(false);updateSelection();});
        connect(m_grid,&QTableView::pressed,this,[this]{m_addressPending=false;m_address->setModified(false);updateSelection();});
        connect(m_address,&QLineEdit::textEdited,this,[this]{m_addressPending=true;m_insert->setEnabled(false);m_status->setText(tr("Press Enter to validate and select this address, or click the grid."));});
        connect(m_model,&SheetModel::externalChange,this,[this]{m_expired=true;m_insert->setEnabled(false);m_status->setText(tr("Workbook changed. Close this picker, refresh and try again."));});connect(m_model,&SheetModel::issue,m_status,&QLabel::setText);
        connect(m_sheet,&QComboBox::currentIndexChanged,this,[this,workbook,state]{m_model->bind(workbook,m_sheet->currentData().toString(),state,true);const auto cell=m_model->index(0,0);m_grid->setCurrentIndex(cell);m_grid->selectionModel()->select(cell,QItemSelectionModel::ClearAndSelect);updateSelection();});
        connect(m_address,&QLineEdit::returnPressed,this,[this]{
            static const QRegularExpression syntax("^([A-Za-z]{1,3})([1-9][0-9]{0,6})(?::([A-Za-z]{1,3})([1-9][0-9]{0,6}))?$");const auto match=syntax.match(m_address->text().trimmed());
            auto column=[](const QString &text){int result=0;for(const auto ch:text.toUpper())result=result*26+ch.unicode()-'A'+1;return result;};
            const int r1=match.captured(2).toInt(),c1=column(match.captured(1)),r2=match.captured(4).isEmpty()?r1:match.captured(4).toInt(),c2=match.captured(3).isEmpty()?c1:column(match.captured(3));
            if(!match.hasMatch() || qMax(r1,r2)>1048576 || qMax(c1,c2)>16384 || qint64(qAbs(r2-r1)+1)*(qAbs(c2-c1)+1)>10000){m_addressPending=true;m_insert->setEnabled(false);m_status->setText(tr("Enter a valid A1 rectangle of at most 10,000 cells within the supported grid."));return;}
            m_addressPending=false;m_address->setModified(false);const auto first=m_model->index(qMin(r1,r2)-1,qMin(c1,c2)-1),last=m_model->index(qMax(r1,r2)-1,qMax(c1,c2)-1);m_grid->setCurrentIndex(first);m_grid->selectionModel()->select(QItemSelection(first,last),QItemSelectionModel::ClearAndSelect);m_grid->scrollTo(first);updateSelection();
        });
        m_model->bind(workbook,m_sheet->currentData().toString(),state,true);m_grid->setCurrentIndex(m_model->index(0,0));m_grid->selectionModel()->select(m_model->index(0,0),QItemSelectionModel::ClearAndSelect);updateSelection();
    }
    QString sheet()const{return m_sheet->currentData().toString();}
    QJsonObject range()const {if(m_addressPending)return {};const auto selected=m_grid->selectionModel()->selection();if(selected.size()!=1)return {};const auto r=selected.first();if(qint64(r.height())*r.width()>10000)return {};return {{"r1",r.top()+1},{"c1",r.left()+1},{"r2",r.bottom()+1},{"c2",r.right()+1}};}
    QString reference()const {const auto r=range();if(r.isEmpty())return {};QString name=m_sheet->currentText();name.replace("'","''");const auto first=SheetModel::columnName(r["c1"].toInt())+QString::number(r["r1"].toInt());const auto last=SheetModel::columnName(r["c2"].toInt())+QString::number(r["r2"].toInt());return "'"+name+"'!"+first+(first==last?QString():":"+last);}
private:
    void updateSelection(){const auto selected=range();m_insert->setEnabled(!m_expired && !selected.isEmpty());if(m_expired)return;if(selected.isEmpty()){m_status->setText(tr("Select one rectangle of at most 10,000 cells."));return;}m_status->setText(reference());if(!m_address->hasFocus() || !m_address->isModified()){auto address=reference().section('!',-1);m_address->setText(address);m_address->setModified(false);}}
    SheetModel *m_model;QTableView *m_grid;QComboBox *m_sheet;QLineEdit *m_address;QLabel *m_status;QPushButton *m_insert;bool m_expired=false,m_addressPending=false;
};
