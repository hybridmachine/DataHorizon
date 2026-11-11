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
            {"style.font",QT_TR_NOOP("Font")},{"style.named_style",QT_TR_NOOP("Named cell style")},{"style.number_format",QT_TR_NOOP("Number format")},
            {"sheet.feature",QT_TR_NOOP("Sheet feature")},{"workbook.feature",QT_TR_NOOP("Workbook feature")}};
        const auto found=labels.constFind(category);
        return found==labels.constEnd()?category:tr(found.value());
    }
    static bool isFeature(const QString &category) { return category=="sheet.feature"||category=="workbook.feature"; }
    // The detail of a feature entry is a stable feature id (service xlsx_features / ods_features).
    static QString featureLabel(const QString &feature) {
        static const QHash<QString,const char *> labels{
            {"merged_cells",QT_TR_NOOP("Merged cells")},{"column_widths",QT_TR_NOOP("Column widths")},{"row_heights",QT_TR_NOOP("Row heights")},
            {"hidden_rows_columns",QT_TR_NOOP("Hidden rows or columns")},{"outline",QT_TR_NOOP("Row or column groups")},
            {"row_column_styles",QT_TR_NOOP("Row or column formatting")},{"sheet_format_defaults",QT_TR_NOOP("Default row height or column width")},
            {"frozen_panes",QT_TR_NOOP("Frozen panes")},{"split_panes",QT_TR_NOOP("Split panes")},{"sheet_view",QT_TR_NOOP("Sheet view settings")},
            {"custom_views",QT_TR_NOOP("Custom views")},{"tab_colour",QT_TR_NOOP("Sheet tab colour")},
            {"conditional_formatting",QT_TR_NOOP("Conditional formatting")},{"data_validation",QT_TR_NOOP("Data validation")},
            {"hyperlinks",QT_TR_NOOP("Hyperlinks")},{"comments",QT_TR_NOOP("Comments")},{"rich_text",QT_TR_NOOP("Formatted text runs")},
            {"rich_values",QT_TR_NOOP("Pictures or data types in cells")},{"phonetic",QT_TR_NOOP("Phonetic guides")},
            {"excel_tables",QT_TR_NOOP("Excel tables")},{"auto_filter",QT_TR_NOOP("AutoFilter")},{"sort_state",QT_TR_NOOP("Saved sort")},
            {"sheet_protection",QT_TR_NOOP("Sheet protection")},{"protected_ranges",QT_TR_NOOP("Protected ranges")},
            {"scenarios",QT_TR_NOOP("Scenarios")},{"data_consolidation",QT_TR_NOOP("Data consolidation")},
            {"page_setup",QT_TR_NOOP("Page and print settings")},{"drawings",QT_TR_NOOP("Drawings, pictures and charts")},
            {"legacy_drawings",QT_TR_NOOP("Shapes or form controls")},{"background_image",QT_TR_NOOP("Sheet background picture")},
            {"ole_objects",QT_TR_NOOP("Embedded OLE objects")},{"form_controls",QT_TR_NOOP("Form controls")},{"sparklines",QT_TR_NOOP("Sparklines")},
            {"slicers",QT_TR_NOOP("Slicers and timelines")},{"cell_watches",QT_TR_NOOP("Cell watches")},
            {"ignored_errors",QT_TR_NOOP("Ignored error indicators")},{"smart_tags",QT_TR_NOOP("Smart tags")},
            {"custom_properties",QT_TR_NOOP("Custom sheet properties")},{"web_publish",QT_TR_NOOP("Web publishing settings")},
            {"extensions",QT_TR_NOOP("Extension data")},{"defined_names",QT_TR_NOOP("Defined names")},
            {"external_references",QT_TR_NOOP("Links to other files")},{"hidden_sheets",QT_TR_NOOP("Hidden sheets")},
            {"workbook_protection",QT_TR_NOOP("Workbook protection")},{"file_sharing",QT_TR_NOOP("File sharing settings")},
            {"calculation_settings",QT_TR_NOOP("Calculation settings")},{"function_groups",QT_TR_NOOP("Macro function groups")},
            {"data_model",QT_TR_NOOP("Data model")},{"charts",QT_TR_NOOP("Charts")},{"pivot_tables",QT_TR_NOOP("Pivot tables")},
            {"vba",QT_TR_NOOP("VBA macros")},{"custom_xml",QT_TR_NOOP("Custom XML data")},{"connections",QT_TR_NOOP("Data connections")},
            {"query_tables",QT_TR_NOOP("Query tables")},{"embedded_objects",QT_TR_NOOP("Embedded objects")},
            {"document_properties",QT_TR_NOOP("Custom document properties")},{"tracked_changes",QT_TR_NOOP("Tracked changes")},
            {"label_ranges",QT_TR_NOOP("Label ranges")},{"macros",QT_TR_NOOP("Macros")}};
        const auto found=labels.constFind(feature);
        return found==labels.constEnd()?feature:tr(found.value());
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
                const auto category=entry["category"].toString(), detail=plain(entry["detail"].toString());
                const auto shown=isFeature(category)?featureLabel(detail):detail;
                auto *row=new QTreeWidgetItem(group,{categoryLabel(category),shown,QString::number(qint64(entry["count"].toDouble()))});
                row->setData(0,Qt::UserRole,category); row->setData(1,Qt::UserRole,detail);
                // The detail elides in its column; the whole text is in the
                // tooltip, escaped so markup from the file shows literally.
                row->setToolTip(1,plainTip(shown==detail?detail:shown+QStringLiteral(" (")+detail+QStringLiteral(")")));
                row->setToolTip(0,plainTip(entry["category"].toString()));
                for(const auto &sampleValue:entry["samples"].toArray()) {
                    const auto sample=sampleValue.toObject();
                    const auto sheet=sample["sheet"].toString(), cell=sample["cell"].toString();
                    // A sample without a cell stands for a whole sheet (or, without a sheet, the workbook).
                    const auto where=sheet.isEmpty()?(cell.isEmpty()?tr("Workbook"):cell):cell.isEmpty()?sheet:sheet+"!"+cell;
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
