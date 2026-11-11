// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QPushButton>
#include <QSignalBlocker>
#include <QVBoxLayout>

// Lists workbook names in the service's shape ({id, name, reference, broken, ...})
// and asks for changes through signals. It never changes names itself: the
// owning window validates the selection and submits managed operations.
class NamesPanel : public QWidget {
    Q_OBJECT
public:
    static constexpr int NameRole=Qt::UserRole+1;

    explicit NamesPanel(QWidget *parent=nullptr):QWidget(parent) {
        setObjectName("namesPanel");
        auto *layout=new QVBoxLayout(this);layout->setContentsMargins(0,0,0,0);
        m_list=new QListWidget;m_list->setObjectName("namesList");m_list->setAccessibleName(tr("Workbook names"));layout->addWidget(m_list,1);
        m_empty=new QLabel(tr("No names yet. Select cells in the workbook window and create a name from the selection."));m_empty->setObjectName("namesEmptyHint");m_empty->setWordWrap(true);layout->addWidget(m_empty);
        m_new=new QPushButton(tr("New name from selection…"));m_new->setObjectName("newNameButton");layout->addWidget(m_new);
        auto *row=new QHBoxLayout;layout->addLayout(row);
        m_rename=new QPushButton(tr("Rename…"));m_rename->setObjectName("renameNameButton");row->addWidget(m_rename);
        m_retarget=new QPushButton(tr("Retarget to selection"));m_retarget->setObjectName("retargetNameButton");row->addWidget(m_retarget);
        m_delete=new QPushButton(tr("Delete…"));m_delete->setObjectName("deleteNameButton");row->addWidget(m_delete);
        m_message=new QLabel;m_message->setObjectName("namesMessage");m_message->setAccessibleName(tr("Name messages"));m_message->setWordWrap(true);
        m_message->setTextInteractionFlags(Qt::TextSelectableByMouse);layout->addWidget(m_message);
        connect(m_list,&QListWidget::itemDoubleClicked,this,[this](QListWidgetItem *item){emit insertRequested(item->data(NameRole).toString());});
        connect(m_list,&QListWidget::currentItemChanged,this,[this]{updateState();emit currentChanged();});
        connect(m_new,&QPushButton::clicked,this,&NamesPanel::createRequested);
        connect(m_rename,&QPushButton::clicked,this,[this]{if(!currentId().isEmpty())emit editRequested(currentId());});
        connect(m_retarget,&QPushButton::clicked,this,[this]{if(!currentId().isEmpty())emit retargetRequested(currentId());});
        connect(m_delete,&QPushButton::clicked,this,[this]{if(!currentId().isEmpty())emit deleteRequested(currentId());});
        updateState();
    }
    void setNames(const QJsonArray &names) {
        const auto keep=currentId();
        {
            QSignalBlocker blocker(m_list);m_list->clear();
            for(const auto &value:names) {
                const auto name=value.toObject();const bool broken=name["broken"].toBool();
                auto *item=new QListWidgetItem(tr("%1 — %2").arg(name["name"].toString(),broken?tr("Broken"):name["reference"].toString()),m_list);
                item->setData(Qt::UserRole,name["id"].toString());item->setData(NameRole,name["name"].toString());
                item->setToolTip(broken?tr("This name no longer refers to any cells. Retarget it to a selection or delete it."):name["reference"].toString());
                if(broken){auto font=item->font();font.setItalic(true);item->setFont(font);}
                if(name["id"].toString()==keep)m_list->setCurrentItem(item);
            }
        }
        m_empty->setVisible(names.isEmpty());updateState();emit currentChanged();
    }
    // Changes are offered only while the workbook can accept them; the reason
    // explains disabled controls.
    void setWritable(bool writable,const QString &reason){m_writable=writable;m_reason=reason;updateState();}
    void showMessage(const QString &text){m_message->setText(text);}
    QString currentId() const{return m_list->currentItem()?m_list->currentItem()->data(Qt::UserRole).toString():QString();}
signals:
    void insertRequested(QString name);
    void createRequested();
    void editRequested(QString nameId);
    void retargetRequested(QString nameId);
    void deleteRequested(QString nameId);
    void currentChanged();
private:
    void updateState() {
        const bool chosen=m_list->currentItem();const QString choose=tr("Choose a name in the list first.");
        m_new->setEnabled(m_writable);m_new->setToolTip(m_writable?tr("Name the selected cells in the workbook window"):m_reason);
        for(auto *button:{m_rename,m_retarget,m_delete}){button->setEnabled(m_writable && chosen);if(!m_writable)button->setToolTip(m_reason);else if(!chosen)button->setToolTip(choose);}
        if(m_writable && chosen) {
            m_rename->setToolTip(tr("Give this name a different identifier"));
            m_retarget->setToolTip(tr("Make this name refer to the cells selected in the workbook window"));
            m_delete->setToolTip(tr("Delete this name from the workbook"));
        }
    }
    QListWidget *m_list;
    QLabel *m_empty,*m_message;
    QPushButton *m_new,*m_rename,*m_retarget,*m_delete;
    bool m_writable=false;
    QString m_reason;
};

// The main window's Name Manager: the shared names list plus the selected
// name's identifier and reference. It is not modal, so the grid selection can
// change while it is open; every change still goes through the owning window.
class NameManagerDialog : public QDialog {
    Q_OBJECT
public:
    explicit NameManagerDialog(QWidget *parent=nullptr):QDialog(parent) {
        setObjectName("nameManagerDialog");setWindowTitle(tr("Name manager"));resize(520,420);
        auto *layout=new QVBoxLayout(this);
        auto *hint=new QLabel(tr("Names refer to one cell or rectangle and can be used by Python code and the address box. Double-click a name to go to it."));hint->setWordWrap(true);layout->addWidget(hint);
        m_panel=new NamesPanel;layout->addWidget(m_panel,1);
        auto *form=new QFormLayout;layout->addLayout(form);
        m_name=new QLineEdit;m_name->setObjectName("nameManagerName");m_name->setReadOnly(true);m_name->setAccessibleName(tr("Selected name"));form->addRow(tr("Name"),m_name);
        m_reference=new QLineEdit;m_reference->setObjectName("nameManagerReference");m_reference->setReadOnly(true);m_reference->setAccessibleName(tr("Selected name's reference"));form->addRow(tr("Refers to"),m_reference);
        auto *buttons=new QDialogButtonBox(QDialogButtonBox::Close);buttons->button(QDialogButtonBox::Close)->setObjectName("closeNameManagerButton");layout->addWidget(buttons);
        connect(buttons,&QDialogButtonBox::rejected,this,&QDialog::reject);
        connect(m_panel,&NamesPanel::currentChanged,this,&NameManagerDialog::showCurrent);
    }
    NamesPanel *panel() const{return m_panel;}
    void setNames(const QJsonArray &names){m_names=names;m_panel->setNames(names);}
private:
    void showCurrent() {
        const auto id=m_panel->currentId();QJsonObject name;
        for(const auto &value:std::as_const(m_names))if(value.toObject()["id"].toString()==id)name=value.toObject();
        m_name->setText(name["name"].toString());
        m_reference->setText(name.isEmpty()?QString():name["broken"].toBool()?tr("Broken: no longer refers to any cells"):name["reference"].toString());
    }
    NamesPanel *m_panel;
    QLineEdit *m_name,*m_reference;
    QJsonArray m_names;
};
