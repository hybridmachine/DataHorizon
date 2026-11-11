// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QCheckBox>
#include <QComboBox>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QSignalBlocker>
#include <QTableWidget>
#include <QVBoxLayout>

// Edits one macro declaration in the service's shape:
// {name, parameters:[{name,type,nullable,default?}], result:{type,nullable}}.
// Declarations are typed explicitly; nothing is inferred from source.
class MacroDeclarationPanel : public QWidget {
    Q_OBJECT
public:
    // The scalar types the service accepts (relational.TYPES).
    static QStringList scalarTypes(){return {"text","integer","number","boolean","date","time","datetime"};}

    explicit MacroDeclarationPanel(QWidget *parent=nullptr):QWidget(parent) {
        setObjectName("macroPanel");
        auto *layout=new QVBoxLayout(this);layout->setContentsMargins(0,0,0,0);
        auto *form=new QFormLayout;layout->addLayout(form);
        m_name=new QLineEdit;m_name->setObjectName("macroName");m_name->setMaxLength(64);m_name->setAccessibleName(tr("Macro function name"));
        m_name->setToolTip(tr("The name of a top-level function in this module."));form->addRow(tr("Function"),m_name);
        m_result=typeBox("macroResultType");m_result->setAccessibleName(tr("Result type"));
        m_resultNullable=new QCheckBox(tr("May be empty"));m_resultNullable->setObjectName("macroResultNullable");m_resultNullable->setAccessibleName(tr("Result may be empty"));
        auto *result=new QHBoxLayout;result->addWidget(m_result,1);result->addWidget(m_resultNullable);form->addRow(tr("Result"),result);
        layout->addWidget(new QLabel(tr("Arguments (after ctx)")));
        m_arguments=new QTableWidget(0,4);m_arguments->setObjectName("macroParameters");m_arguments->setAccessibleName(tr("Macro arguments"));
        m_arguments->setHorizontalHeaderLabels({tr("Name"),tr("Type"),tr("May be empty"),tr("Default")});
        m_arguments->horizontalHeader()->setSectionResizeMode(0,QHeaderView::Stretch);m_arguments->horizontalHeader()->setSectionResizeMode(3,QHeaderView::Stretch);
        m_arguments->verticalHeader()->hide();m_arguments->setSelectionBehavior(QAbstractItemView::SelectRows);m_arguments->setSelectionMode(QAbstractItemView::SingleSelection);
        layout->addWidget(m_arguments,1);
        auto *buttons=new QHBoxLayout;layout->addLayout(buttons);
        m_add=new QPushButton(tr("Add argument"));m_add->setObjectName("addArgumentButton");buttons->addWidget(m_add);
        m_remove=new QPushButton(tr("Remove argument"));m_remove->setObjectName("removeArgumentButton");buttons->addWidget(m_remove);buttons->addStretch();
        connect(m_add,&QPushButton::clicked,this,[this]{addArgument({{"name",QString("arg%1").arg(m_arguments->rowCount()+1)},{"type","text"},{"nullable",false}});emit changed();});
        connect(m_remove,&QPushButton::clicked,this,[this]{
            const int row=m_arguments->currentRow()>=0?m_arguments->currentRow():m_arguments->rowCount()-1;if(row<0)return;
            m_arguments->removeRow(row);renumber();emit changed();});
        connect(m_name,&QLineEdit::textChanged,this,[this]{if(!m_loading)emit changed();});
        connect(m_result,&QComboBox::currentIndexChanged,this,[this]{if(!m_loading)emit changed();});
        connect(m_resultNullable,&QCheckBox::toggled,this,[this]{if(!m_loading)emit changed();});
        setDeclaration({});
    }

    void setDeclaration(const QJsonObject &declaration) {
        m_loading=true;
        m_name->setText(declaration["name"].toString());
        const auto result=declaration["result"].toObject();
        m_result->setCurrentIndex(qMax(0,m_result->findData(result["type"].toString("text"))));
        m_resultNullable->setChecked(result["nullable"].toBool());
        m_arguments->setRowCount(0);
        for(const auto &value:declaration["parameters"].toArray())addArgument(value.toObject());
        m_loading=false;
    }

    QJsonObject declaration() const {
        QJsonArray parameters;
        for(int row=0;row<m_arguments->rowCount();++row) {
            const auto *type=qobject_cast<QComboBox *>(m_arguments->cellWidget(row,1));
            const auto *nullable=qobject_cast<QCheckBox *>(m_arguments->cellWidget(row,2));
            const auto *fallback=qobject_cast<QLineEdit *>(m_arguments->cellWidget(row,3));
            QJsonObject parameter{{"name",qobject_cast<QLineEdit *>(m_arguments->cellWidget(row,0))->text().trimmed()},{"type",type->currentData().toString()},{"nullable",nullable->isChecked()}};
            if(!fallback->text().isEmpty())parameter["default"]=defaultValue(type->currentData().toString(),nullable->isChecked(),fallback->text());
            parameters.append(parameter);
        }
        return {{"name",m_name->text().trimmed()},{"parameters",parameters},{"result",QJsonObject{{"type",m_result->currentData().toString()},{"nullable",m_resultNullable->isChecked()}}}};
    }

    QStringList argumentNames() const {
        QStringList names;for(const auto &value:declaration()["parameters"].toArray())names.append(value.toObject()["name"].toString());return names;
    }
signals:
    void changed();
private:
    QComboBox *typeBox(const QString &name) const {
        auto *box=new QComboBox;box->setObjectName(name);
        const QStringList labels{tr("Text"),tr("Integer"),tr("Number"),tr("Boolean"),tr("Date"),tr("Time"),tr("Date and time")};
        const auto types=scalarTypes();for(int i=0;i<types.size();++i)box->addItem(labels[i],types[i]);
        return box;
    }
    // An empty Default cell means "no default". Values are converted to the
    // declared type where they parse; otherwise the text is sent as typed
    // so the service reports the authoritative type error.
    static QJsonValue defaultValue(const QString &type,bool nullable,const QString &text) {
        if(nullable && text==QLatin1String("None"))return QJsonValue::Null;
        bool ok=false;
        if(type=="integer"){const auto value=text.trimmed().toLongLong(&ok);if(ok)return QJsonValue(qint64(value));}
        else if(type=="number"){const auto value=text.trimmed().toDouble(&ok);if(ok)return value;}
        else if(type=="boolean"){const auto value=text.trimmed().toLower();if(value=="true")return true;if(value=="false")return false;}
        return text;
    }
    static QString defaultText(const QJsonValue &value) {
        if(value.isNull())return QStringLiteral("None");
        if(value.isBool())return value.toBool()?QStringLiteral("true"):QStringLiteral("false");
        if(value.isDouble()){const double number=value.toDouble();return number==qint64(number) && qAbs(number)<1e15?QString::number(qint64(number)):QString::number(number,'g',17);}
        return value.toString();
    }
    void addArgument(const QJsonObject &parameter) {
        const int row=m_arguments->rowCount();m_arguments->insertRow(row);
        auto *name=new QLineEdit(parameter["name"].toString());name->setMaxLength(64);name->setAccessibleName(tr("Argument name"));
        auto *type=typeBox({});type->setCurrentIndex(qMax(0,type->findData(parameter["type"].toString("text"))));type->setAccessibleName(tr("Argument type"));
        auto *nullable=new QCheckBox;nullable->setChecked(parameter["nullable"].toBool());nullable->setAccessibleName(tr("Argument may be empty"));
        auto *fallback=new QLineEdit(parameter.contains("default")?defaultText(parameter["default"]):QString());fallback->setAccessibleName(tr("Argument default"));
        fallback->setPlaceholderText(tr("No default"));fallback->setToolTip(tr("Leave empty for no default. Type None for an empty default when the argument may be empty."));
        m_arguments->setCellWidget(row,0,name);m_arguments->setCellWidget(row,1,type);m_arguments->setCellWidget(row,2,nullable);m_arguments->setCellWidget(row,3,fallback);
        connect(name,&QLineEdit::textChanged,this,[this]{if(!m_loading)emit changed();});
        connect(fallback,&QLineEdit::textChanged,this,[this]{if(!m_loading)emit changed();});
        connect(type,&QComboBox::currentIndexChanged,this,[this]{if(!m_loading)emit changed();});
        connect(nullable,&QCheckBox::toggled,this,[this]{if(!m_loading)emit changed();});
        renumber();
    }
    void renumber() {
        for(int row=0;row<m_arguments->rowCount();++row) {
            const auto suffix=QString::number(row);
            m_arguments->cellWidget(row,0)->setObjectName("argumentName_"+suffix);m_arguments->cellWidget(row,1)->setObjectName("argumentType_"+suffix);
            m_arguments->cellWidget(row,2)->setObjectName("argumentNullable_"+suffix);m_arguments->cellWidget(row,3)->setObjectName("argumentDefault_"+suffix);
        }
    }
    QLineEdit *m_name;
    QComboBox *m_result;
    QCheckBox *m_resultNullable;
    QTableWidget *m_arguments;
    QPushButton *m_add,*m_remove;
    bool m_loading=false;
};
