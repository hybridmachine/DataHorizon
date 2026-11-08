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
#include <QSpinBox>
#include <QFontDatabase>
#include <QCompleter>
#include <QVBoxLayout>

class FormatCellsDialog : public QDialog {
public:
    explicit FormatCellsDialog(const QString &range,QWidget *parent=nullptr):QDialog(parent) {
        setObjectName("formatCellsDialog");setWindowTitle(tr("Format Cells"));resize(600,440);auto *layout=new QVBoxLayout(this);auto *scope=new QLabel(tr("Apply formatting to %1. Stored inputs and formulas stay unchanged.").arg(range),this);scope->setTextFormat(Qt::PlainText);scope->setWordWrap(true);layout->addWidget(scope);
        m_tabs=new QTabWidget(this);m_tabs->setObjectName("formatTabs");layout->addWidget(m_tabs,1);
        auto tab=[this](const QString &title){auto *page=new QWidget(m_tabs);auto *form=new QFormLayout(page);m_tabs->addTab(page,title);return form;};
        auto *number=tab(tr("Number"));m_format=combo("numberFormat",number,tr("Category"),{{tr("General"),"general"},{tr("Text"),"text"},{tr("Number"),"number"},{tr("Percentage"),"percent"},{tr("Currency"),"currency"}});
        m_decimals=new QSpinBox(this);m_decimals->setObjectName("decimalPlaces");m_decimals->setRange(-1,15);m_decimals->setSpecialValueText(tr("Keep existing"));m_decimals->setValue(-1);number->addRow(tr("Decimal places (default 2)"),m_decimals);connect(m_format,&QComboBox::currentIndexChanged,this,[this]{const bool numeric=!QStringList{"general","text"}.contains(m_format->currentData().toString());m_decimals->setEnabled(numeric);m_grouping->setEnabled(numeric);m_currency->setEnabled(m_format->currentData()=="currency");validate();});
        m_grouping=check("numberGrouping",number,tr("Thousands separator (comma)"));
        m_currency=new QLineEdit("USD",this);m_currency->setObjectName("currencyCode");m_currency->setMaxLength(3);m_currency->setEnabled(false);m_currency->setAccessibleName(tr("Three-letter currency label"));number->addRow(tr("Currency label"),m_currency);connect(m_currency,&QLineEdit::textChanged,this,[this]{validate();});
        auto *font=tab(tr("Font"));m_bold=check("fontBold",font,tr("Bold"));m_italic=check("fontItalic",font,tr("Italic"));
        m_family=new QLineEdit(this);m_family->setObjectName("fontFamily");m_family->setMaxLength(64);m_family->setPlaceholderText(tr("Keep existing"));m_family->setCompleter(new QCompleter(QFontDatabase::families(),m_family));font->addRow(tr("Font family"),m_family);
        m_size=new QSpinBox(this);m_size->setObjectName("fontSize");m_size->setRange(5,72);m_size->setSpecialValueText(tr("Keep existing"));m_size->setValue(5);font->addRow(tr("Size (points)"),m_size);
        m_textColor=new QLineEdit(this);m_textColor->setObjectName("textColor");m_textColor->setMaxLength(7);m_textColor->setPlaceholderText(tr("Keep existing, or #RRGGBB"));font->addRow(tr("Text RGB color"),m_textColor);auto *ink=new QPushButton(tr("Choose text color…"),this);ink->setAutoDefault(false);font->addRow(ink);connect(ink,&QPushButton::clicked,this,[this]{const auto c=QColorDialog::getColor(QColor(m_textColor->text()),this,tr("Text color"));if(c.isValid())m_textColor->setText(c.name(QColor::HexRgb));});
        auto *alignment=tab(tr("Alignment"));m_align=combo("cellAlignment",alignment,tr("Horizontal"),{{tr("Left"),"left"},{tr("Center"),"center"},{tr("Right"),"right"}});m_wrap=check("cellWrap",alignment,tr("Wrap text"));
        auto *fill=tab(tr("Fill"));m_background=new QLineEdit(this);m_background->setObjectName("cellBackground");m_background->setAccessibleName(tr("Background RGB color"));m_background->setPlaceholderText(tr("Keep existing, or #RRGGBB"));m_background->setMaxLength(7);fill->addRow(tr("Background"),m_background);auto *choose=new QPushButton(tr("Choose color…"),this);choose->setAutoDefault(false);fill->addRow(choose);connect(choose,&QPushButton::clicked,this,[this]{const auto color=QColorDialog::getColor(QColor(m_background->text()),this,tr("Cell background"));if(color.isValid())m_background->setText(color.name(QColor::HexRgb));});
        auto *border=tab(tr("Border"));m_border=combo("cellBorder",border,tr("Borders on each cell"),{{tr("None"),"none"},{tr("All edges (thin)"),"box"}});
        auto *help=new QLabel(tr("Keep existing and partially checked boxes preserve each cell's formatting. Choose only the properties you want to change."),this);help->setWordWrap(true);layout->addWidget(help);
        m_reset=new QCheckBox(tr("Clear all existing formatting before applying these choices"),this);m_reset->setObjectName("resetAllFormats");layout->addWidget(m_reset);
        m_error=new QLabel(this);m_error->setObjectName("formatValidation");m_error->setWordWrap(true);layout->addWidget(m_error);m_buttons=new QDialogButtonBox(QDialogButtonBox::Ok|QDialogButtonBox::Cancel,this);layout->addWidget(m_buttons);connect(m_buttons,&QDialogButtonBox::accepted,this,&QDialog::accept);connect(m_buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);connect(m_background,&QLineEdit::textChanged,this,[this]{validate();});connect(m_family,&QLineEdit::textChanged,this,[this]{validate();});connect(m_textColor,&QLineEdit::textChanged,this,[this]{validate();});validate();
    }
    QJsonObject patch()const {
        QJsonObject result;for(const auto &pair:QList<QPair<QString,QComboBox *>>{{"format",m_format},{"align",m_align},{"border",m_border}})if(!pair.second->currentData().toString().isEmpty())result[pair.first]=pair.second->currentData().toString();
        for(const auto &pair:QList<QPair<QString,QCheckBox *>>{{"bold",m_bold},{"italic",m_italic},{"wrap",m_wrap}})if(pair.second->checkState()!=Qt::PartiallyChecked)result[pair.first]=pair.second->isChecked();
        if(!m_family->text().isEmpty())result["font_family"]=m_family->text();
        if(m_size->value()>=6)result["font_size"]=m_size->value();
        if(!m_textColor->text().isEmpty())result["text_color"]=m_textColor->text().toLower();
        if(m_currency->isEnabled())result["currency"]=m_currency->text();
        if(m_grouping->isEnabled() && m_grouping->checkState()!=Qt::PartiallyChecked)result["grouping"]=m_grouping->isChecked();
        if(m_decimals->isEnabled() && m_decimals->value()>=0)result["decimals"]=m_decimals->value();
        if(!m_background->text().trimmed().isEmpty())result["background"]=m_background->text().trimmed().toLower();return result;
    }
    bool replaceStyle()const{return m_reset->isChecked();}
private:
    QComboBox *combo(const QString &name,QFormLayout *layout,const QString &label,const QList<QPair<QString,QString>> &choices){auto *field=new QComboBox(this);field->setObjectName(name);field->setAccessibleName(label);field->addItem(tr("Keep existing"),"");for(const auto &choice:choices)field->addItem(choice.first,choice.second);layout->addRow(label,field);return field;}
    QCheckBox *check(const QString &name,QFormLayout *layout,const QString &label){auto *field=new QCheckBox(label,this);field->setObjectName(name);field->setTristate(true);field->setCheckState(Qt::PartiallyChecked);field->setAccessibleDescription(tr("Partially checked keeps existing formatting"));layout->addRow(field);return field;}
    void validate(){const auto value=m_background->text().trimmed();const bool color=value.isEmpty() || QRegularExpression("^#[0-9a-fA-F]{6}$").match(value).hasMatch();const bool currency=!m_currency->isEnabled() || QRegularExpression("^[A-Z]{3}$").match(m_currency->text()).hasMatch();const bool textColor=m_textColor->text().isEmpty() || QRegularExpression("^#[0-9a-fA-F]{6}$").match(m_textColor->text()).hasMatch();const auto family=m_family->text();const bool familyValid=family.isEmpty() || (family==family.trimmed() && !family.contains(QRegularExpression("[\\x00-\\x1f\\x7f,\\\\\"']")));const bool valid=color && currency && textColor && familyValid;m_error->setText(!color?tr("Enter a background color as #RRGGBB, or leave it empty to keep existing fills."):!currency?tr("Enter three uppercase letters for the currency label, such as USD or EUR."):!textColor?tr("Enter text color as #RRGGBB."):!familyValid?tr("Use one font family without controls, quotes, comma or backslash."):QString());m_buttons->button(QDialogButtonBox::Ok)->setEnabled(valid);}

    QSpinBox *m_size,*m_decimals;QTabWidget *m_tabs;QComboBox *m_format,*m_align,*m_border;QCheckBox *m_grouping,*m_bold,*m_italic,*m_wrap,*m_reset;QLineEdit *m_family,*m_textColor,*m_currency,*m_background;QLabel *m_error;QDialogButtonBox *m_buttons;
};
