// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QCheckBox>
#include <QColorDialog>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QRegularExpression>
#include <QTabWidget>
#include <QVBoxLayout>

class FormatCellsDialog : public QDialog {
public:
    explicit FormatCellsDialog(const QString &range,QWidget *parent=nullptr):QDialog(parent) {
        setObjectName("formatCellsDialog");setWindowTitle(tr("Format Cells"));resize(600,440);auto *layout=new QVBoxLayout(this);auto *scope=new QLabel(tr("Apply formatting to %1. Stored inputs and formulas stay unchanged.").arg(range),this);scope->setTextFormat(Qt::PlainText);scope->setWordWrap(true);layout->addWidget(scope);
        m_tabs=new QTabWidget(this);m_tabs->setObjectName("formatTabs");layout->addWidget(m_tabs,1);
        auto tab=[this](const QString &title){auto *page=new QWidget(m_tabs);auto *form=new QFormLayout(page);m_tabs->addTab(page,title);return form;};
        auto *number=tab(tr("Number"));m_format=combo("numberFormat",number,tr("Category"),{{tr("General"),"general"},{tr("Text"),"text"},{tr("Number (2 decimal places)"),"fixed2"},{tr("Percentage (2 decimal places)"),"percent2"}});
        auto *font=tab(tr("Font"));m_bold=check("fontBold",font,tr("Bold"));m_italic=check("fontItalic",font,tr("Italic"));
        auto *alignment=tab(tr("Alignment"));m_align=combo("cellAlignment",alignment,tr("Horizontal"),{{tr("Left"),"left"},{tr("Center"),"center"},{tr("Right"),"right"}});m_wrap=check("cellWrap",alignment,tr("Wrap text"));
        auto *fill=tab(tr("Fill"));m_background=new QLineEdit(this);m_background->setObjectName("cellBackground");m_background->setAccessibleName(tr("Background RGB color"));m_background->setPlaceholderText(tr("Keep existing, or #RRGGBB"));m_background->setMaxLength(7);fill->addRow(tr("Background"),m_background);auto *choose=new QPushButton(tr("Choose color…"),this);choose->setAutoDefault(false);fill->addRow(choose);connect(choose,&QPushButton::clicked,this,[this]{const auto color=QColorDialog::getColor(QColor(m_background->text()),this,tr("Cell background"));if(color.isValid())m_background->setText(color.name(QColor::HexRgb));});
        auto *border=tab(tr("Border"));m_border=combo("cellBorder",border,tr("Borders on each cell"),{{tr("None"),"none"},{tr("All edges (thin)"),"box"}});
        auto *help=new QLabel(tr("Keep existing and partially checked boxes preserve each cell's formatting. Choose only the properties you want to change."),this);help->setWordWrap(true);layout->addWidget(help);
        m_reset=new QCheckBox(tr("Clear all existing formatting before applying these choices"),this);m_reset->setObjectName("resetAllFormats");layout->addWidget(m_reset);
        m_error=new QLabel(this);m_error->setObjectName("formatValidation");m_error->setWordWrap(true);layout->addWidget(m_error);m_buttons=new QDialogButtonBox(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,this);layout->addWidget(m_buttons);connect(m_buttons,&QDialogButtonBox::accepted,this,&QDialog::accept);connect(m_buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);connect(m_background,&QLineEdit::textChanged,this,[this]{validate();});validate();
    }
    QJsonObject patch()const {
        QJsonObject result;for(const auto &pair:QList<QPair<QString,QComboBox *>>{{"format",m_format},{"align",m_align},{"border",m_border}})if(!pair.second->currentData().toString().isEmpty())result[pair.first]=pair.second->currentData().toString();
        for(const auto &pair:QList<QPair<QString,QCheckBox *>>{{"bold",m_bold},{"italic",m_italic},{"wrap",m_wrap}})if(pair.second->checkState()!=Qt::PartiallyChecked)result[pair.first]=pair.second->isChecked();
        if(!m_background->text().trimmed().isEmpty())result["background"]=m_background->text().trimmed().toLower();return result;
    }
    bool replaceStyle()const{return m_reset->isChecked();}
private:
    QComboBox *combo(const QString &name,QFormLayout *layout,const QString &label,const QList<QPair<QString,QString>> &choices){auto *field=new QComboBox(this);field->setObjectName(name);field->setAccessibleName(label);field->addItem(tr("Keep existing"),"");for(const auto &choice:choices)field->addItem(choice.first,choice.second);layout->addRow(label,field);return field;}
    QCheckBox *check(const QString &name,QFormLayout *layout,const QString &label){auto *field=new QCheckBox(label,this);field->setObjectName(name);field->setTristate(true);field->setCheckState(Qt::PartiallyChecked);field->setAccessibleDescription(tr("Partially checked keeps existing formatting"));layout->addRow(field);return field;}
    void validate(){const auto value=m_background->text().trimmed();const bool valid=value.isEmpty() || QRegularExpression("^#[0-9a-fA-F]{6}$").match(value).hasMatch();m_error->setText(valid?QString():tr("Enter a background color as #RRGGBB, or leave it empty to keep existing fills."));m_buttons->button(QDialogButtonBox::Ok)->setEnabled(valid);}
    QTabWidget *m_tabs;QComboBox *m_format,*m_align,*m_border;QCheckBox *m_bold,*m_italic,*m_wrap,*m_reset;QLineEdit *m_background;QLabel *m_error;QDialogButtonBox *m_buttons;
};
