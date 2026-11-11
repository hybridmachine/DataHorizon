// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QHash>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QTreeWidget>
#include <QVBoxLayout>
#include <QWidget>

// A conversion report grouped by severity: unevaluable, omitted, approximated,
// converted. Every text comes from an untrusted file or the service and is
// shown as plain text (tree items never interpret markup); long texts elide.
class ConversionReportView : public QWidget {
    Q_OBJECT
public:
    explicit ConversionReportView(QWidget *parent = nullptr) : QWidget(parent) {
        setObjectName("conversionReport");
        auto *layout=new QVBoxLayout(this); layout->setContentsMargins(0,0,0,0);
        m_tree=new QTreeWidget(this); m_tree->setObjectName("conversionReportTree");
        m_tree->setColumnCount(3);
        m_tree->setHeaderLabels({tr("What"),tr("Detail"),tr("Count")});
        m_tree->setAccessibleName(tr("Conversion report"));
        m_tree->setTextElideMode(Qt::ElideMiddle); m_tree->setWordWrap(false); m_tree->setUniformRowHeights(true);
        m_tree->setRootIsDecorated(true); m_tree->header()->setStretchLastSection(false);
        // Labels fit their text; the detail column takes all remaining width.
        m_tree->header()->setSectionResizeMode(0,QHeaderView::ResizeToContents);
        m_tree->header()->setSectionResizeMode(1,QHeaderView::Stretch);
        m_tree->header()->setSectionResizeMode(2,QHeaderView::ResizeToContents);

        m_totals=new QLabel(this); m_totals->setObjectName("conversionTotals"); m_totals->setTextFormat(Qt::PlainText);
        m_totals->setAccessibleName(tr("Conversion totals"));
        layout->addWidget(m_tree); layout->addWidget(m_totals);
    }
    static QStringList severities() { return {"unevaluable","omitted","approximated","converted"}; }
    static QString severityLabel(const QString &severity) {
        if(severity=="unevaluable") return tr("Formulas kept but not calculated");
        if(severity=="omitted") return tr("Left out");
        if(severity=="approximated") return tr("Approximated");
        if(severity=="converted") return tr("Converted");
        return severity;
    }
    static QString categoryLabel(const QString &category) {
        static const QHash<QString,const char *> labels{
            {"cell.date_conversion",QT_TR_NOOP("Date or time format")},{"cell.error_value",QT_TR_NOOP("Error value")},
            {"cell.invalid_value",QT_TR_NOOP("Invalid cell value")},{"cell.missing_cache",QT_TR_NOOP("Formula without a saved result")},
            {"cell.number_out_of_range",QT_TR_NOOP("Number out of range")},{"cell.text_too_long",QT_TR_NOOP("Text too long")},
            {"cell.unsupported_type",QT_TR_NOOP("Unsupported cell type")},{"formula.array",QT_TR_NOOP("Array formula")},
            {"formula.cache_omitted",QT_TR_NOOP("Saved formula result")},{"formula.data_table",QT_TR_NOOP("Data table formula")},
            {"formula.display_not_evaluated",QT_TR_NOOP("Formula display not calculated")},
            {"formula.external_reference",QT_TR_NOOP("Reference to another file")},{"formula.missing_sheet",QT_TR_NOOP("Reference to a missing sheet")},
            {"formula.name",QT_TR_NOOP("Defined name in a formula")},{"formula.syntax",QT_TR_NOOP("Formula syntax")},
            {"formula.unmaintained",QT_TR_NOOP("Formula not adjusted by edits")},{"formula.unsupported_function",QT_TR_NOOP("Unsupported function")},
            {"formula.unsupported_reference",QT_TR_NOOP("Unsupported reference")},{"formula.unsupported_syntax",QT_TR_NOOP("Unsupported formula syntax")},
            {"formula.untranslatable",QT_TR_NOOP("Formula that cannot be translated")},{"report.truncated",QT_TR_NOOP("More report entries")},
            {"style.alignment",QT_TR_NOOP("Alignment")},{"style.border",QT_TR_NOOP("Border")},{"style.fill",QT_TR_NOOP("Fill")},
            {"style.font",QT_TR_NOOP("Font")},{"style.named_style",QT_TR_NOOP("Named cell style")},{"style.number_format",QT_TR_NOOP("Number format")}};
        const auto found=labels.constFind(category);
        return found==labels.constEnd()?category:tr(found.value());
    }
    // report holds report_entries and totals as the service returns them.
    void setReport(const QJsonObject &report) {
        m_tree->clear();
        const auto entries=report["report_entries"].toArray();
        const auto totals=report["totals"].toObject();
        QStringList totalText;
        for(const auto &severity:severities()) {
            qint64 total=qint64(totals[severity].toDouble());
            if(!totals.contains(severity)) for(const auto &value:entries) if(value.toObject()["severity"]==severity) total+=qint64(value.toObject()["count"].toDouble());
            totalText.append(QStringLiteral("%1 %2").arg(severityLabel(severity)).arg(total));
            if(total<=0) continue;
            auto *group=new QTreeWidgetItem(m_tree,{severityLabel(severity),QString(),QString::number(total)});
            group->setData(0,Qt::UserRole,severity); group->setFirstColumnSpanned(false);
            auto font=group->font(0); font.setBold(true); group->setFont(0,font);
            for(const auto &value:entries) {
                const auto entry=value.toObject(); if(entry["severity"]!=severity) continue;
                auto *row=new QTreeWidgetItem(group,{categoryLabel(entry["category"].toString()),plain(entry["detail"].toString()),QString::number(qint64(entry["count"].toDouble()))});
                row->setData(0,Qt::UserRole,entry["category"].toString());
                // The detail elides in its column; the whole text is in the
                // tooltip, escaped so markup from the file shows literally.
                row->setToolTip(1,plainTip(plain(entry["detail"].toString())));
                row->setToolTip(0,plainTip(entry["category"].toString()));
                for(const auto &sampleValue:entry["samples"].toArray()) {
                    const auto sample=sampleValue.toObject();
                    const auto sheet=sample["sheet"].toString(), cell=sample["cell"].toString();
                    const auto where=sheet.isEmpty()?(cell.isEmpty()?tr("Workbook"):cell):sheet+"!"+cell;
                    auto *location=new QTreeWidgetItem(row,{plain(where)});
                    location->setToolTip(0,plainTip(plain(where)));
                }
            }
            group->setExpanded(true);
        }
        m_tree->setVisible(m_tree->topLevelItemCount()>0);
        m_totals->setText(m_tree->topLevelItemCount()==0?tr("Nothing was changed or left out."):totalText.join(QStringLiteral(" • ")));
    }
    QTreeWidget *tree() const { return m_tree; }
private:
    // One line, bounded: an untrusted text never spreads over many rows.
    // Tooltips interpret markup they detect: escape, then mark as rich text so
    // the escaped characters render literally.
    static QString plainTip(const QString &text) { return QStringLiteral("<qt>%1</qt>").arg(text.toHtmlEscaped()); }
    static QString plain(QString text) {
        text.replace(QLatin1Char('\n'),QLatin1Char(' ')).replace(QLatin1Char('\r'),QLatin1Char(' '));
        return text.left(1024);
    }
    QTreeWidget *m_tree=nullptr;
    QLabel *m_totals=nullptr;
};
