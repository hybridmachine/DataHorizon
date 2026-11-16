// SPDX-License-Identifier: GPL-3.0-only
#include "chartlayer.h"
#include "chartspec.h"
#include "mainwindow.h"
#include <QAccessible>
#include <QAction>
#include <QHash>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonObject>
#include <QLineEdit>
#include <QScrollBar>
#include <QSignalSpy>
#include <QTabBar>
#include <QTableView>
#include <QTemporaryDir>
#include <QtTest>

namespace {
QJsonObject text(const QString &value) { return {{"text", value}}; }
QJsonObject src(const QJsonObject &source) { return {{"source", source}}; }
QJsonObject storedRange(const QString &sheet, const QString &ref, bool broken = false) {
    return {{"kind", "range"}, {"sheet_id", sheet}, {"row1", 2}, {"col1", 1}, {"row2", 13}, {"col2", 1}, {"broken", broken}, {"ref", ref}};
}
QJsonObject storedTable() {
    return {{"kind", "table"}, {"table_id", "t1"}, {"column_id", "c1"}, {"broken", false}, {"table", "Orders"}, {"column", "Total"}};
}
QJsonObject storedQuery() {
    return {{"kind", "query"}, {"query_id", "q1"}, {"column", "Sum"}, {"broken", false}, {"query", "ByRegion"}};
}
QJsonObject series(const QString &name, const QJsonObject &values, const QJsonValue &x = QJsonValue::Null) {
    return {{"name", text(name)}, {"values", src(values)}, {"x", x}};
}
QJsonObject chartWith(const QJsonObject &spec) {
    return {{"id", "ch1"}, {"issue", QJsonValue::Null}, {"over_limit", QJsonValue::Null}, {"spec", spec}};
}
QJsonObject specOf(const QString &type, const QJsonValue &grouping, const QJsonValue &categories, const QJsonArray &seriesList) {
    return {{"spec_version", 1}, {"type", type}, {"grouping", grouping}, {"title", QJsonObject{{"text", QJsonValue::Null}}},
            {"categories", categories}, {"series", seriesList}, {"legend", QJsonObject{{"position", "right"}}}};
}
}

class ChartAuthoringTests : public QObject {
    Q_OBJECT
private slots:
    void inputSpecRebuildsSourcesById() {
        const auto spec = specOf("column", "stacked", src(storedRange("s1", "A2:A13")),
            {series("Sales", storedTable()), QJsonObject{{"name", src(storedQuery())}, {"values", src(storedRange("s1", "B2:B13"))}, {"x", QJsonValue::Null}}});
        QString reason;
        const auto input = ChartSpec::inputSpec(chartWith(spec), &reason);
        QVERIFY2(reason.isEmpty(), qPrintable(reason));
        QCOMPARE(input["categories"].toObject()["source"].toObject(), (QJsonObject{{"kind", "range"}, {"sheet", "s1"}, {"ref", "A2:A13"}}));
        const auto list = input["series"].toArray();
        QCOMPARE(list[0].toObject()["values"].toObject()["source"].toObject(), (QJsonObject{{"kind", "table"}, {"table", "t1"}, {"column", "c1"}}));
        QCOMPARE(list[0].toObject()["name"].toObject(), text("Sales"));
        QCOMPARE(list[1].toObject()["name"].toObject()["source"].toObject(), (QJsonObject{{"kind", "query"}, {"query", "q1"}, {"column", "Sum"}}));
        QCOMPARE(list[1].toObject()["values"].toObject()["source"].toObject(), (QJsonObject{{"kind", "range"}, {"sheet", "s1"}, {"ref", "B2:B13"}}));
        QCOMPARE(input["type"].toString(), QString("column"));
        QCOMPARE(input["grouping"].toString(), QString("stacked"));
        QCOMPARE(input["legend"].toObject()["position"].toString(), QString("right"));
    }
    void inputSpecRefusesBrokenAndUnreadable() {
        QString reason;
        auto broken = specOf("line", QJsonValue::Null, QJsonValue::Null, {series("S", storedRange("s1", "A1:A2", true))});
        QVERIFY(ChartSpec::hasBrokenSource(chartWith(broken)));
        QVERIFY(ChartSpec::inputSpec(chartWith(broken), &reason).isEmpty());
        QVERIFY(!reason.isEmpty());
        QCOMPARE(reason, ChartSpec::stateReason(chartWith(broken)));
        auto ok = specOf("line", QJsonValue::Null, QJsonValue::Null, {series("S", storedRange("s1", "A1:A2"))});
        QVERIFY(!ChartSpec::hasBrokenSource(chartWith(ok)));
        QVERIFY(ChartSpec::stateReason(chartWith(ok)).isEmpty());
        auto unreadable = chartWith(ok);
        unreadable["issue"] = "INVALID_SPEC";
        reason.clear();
        QVERIFY(ChartSpec::inputSpec(unreadable, &reason).isEmpty());
        QVERIFY(reason.contains("INVALID_SPEC"));
        auto over = chartWith(ok);
        over["over_limit"] = "ROW_LIMIT";
        QVERIFY(ChartSpec::stateReason(over).contains("ROW_LIMIT"));
    }
    void typeChangeCompanions_data() {
        QTest::addColumn<QString>("from");
        QTest::addColumn<QString>("to");
        const QStringList types{"column", "bar", "line", "area", "pie", "scatter"};
        for (const auto &from : types)
            for (const auto &to : types)
                if (from != "scatter" || to != "scatter") QTest::newRow(qPrintable(from + "->" + to)) << from << to;
    }
    void typeChangeCompanions() {
        QFETCH(QString, from);
        QFETCH(QString, to);
        const QJsonObject cats = src(storedRange("s1", "A2:A13"));
        const QJsonObject xs1 = src(storedRange("s1", "C2:C13")), xs2 = src(storedRange("s1", "D2:D13"));
        const bool fromScatter = from == "scatter";
        const QJsonValue grouping = ChartSpec::takesGrouping(from) ? QJsonValue("stacked") : QJsonValue();
        const auto spec = fromScatter
            ? specOf(from, grouping, QJsonValue::Null, {series("A", storedRange("s1", "B2:B13"), xs1), series("B", storedRange("s1", "E2:E13"), xs2)})
            : specOf(from, grouping, cats, {series("A", storedRange("s1", "B2:B13")), series("B", storedRange("s1", "E2:E13"))});
        QString refusal;
        const auto out = ChartSpec::withType(spec, to, &refusal);
        if (to == "scatter" && !fromScatter) {
            QVERIFY(!out.isEmpty());
            QVERIFY(out["categories"].isNull());
            QCOMPARE(out["series"].toArray()[0].toObject()["x"].toObject(), cats);
            QCOMPARE(out["series"].toArray()[1].toObject()["x"].toObject(), cats);
        } else if (fromScatter) {
            QCOMPARE(out["categories"].toObject(), xs1);
            for (const auto &item : out["series"].toArray()) QVERIFY(item.toObject()["x"].isNull());
        } else {
            QCOMPARE(out["categories"].toObject(), cats);
            for (const auto &item : out["series"].toArray()) QVERIFY(item.toObject()["x"].isNull());
        }
        QCOMPARE(out["type"].toString(), to);
        QCOMPARE(out["series"].toArray().size(), 2);
        if (!ChartSpec::takesGrouping(to)) QVERIFY(out["grouping"].isNull());
        else if (ChartSpec::takesGrouping(from)) QCOMPARE(out["grouping"].toString(), QString("stacked"));
        else QCOMPARE(out["grouping"].toString(), QString("clustered"));
        QCOMPARE(out["legend"], spec["legend"]);
        QCOMPARE(out["title"], spec["title"]);
    }
    void scatterNeedsAnXSource() {
        const auto spec = specOf("line", QJsonValue::Null, QJsonValue::Null, {series("A", storedRange("s1", "B2:B13"))});
        QString refusal;
        QVERIFY(ChartSpec::withType(spec, "scatter", &refusal).isEmpty());
        QCOMPARE(refusal, QString("Give an X source before changing this chart to a scatter chart."));
    }
    void pieKeepsEverySeries() {
        const auto spec = specOf("column", "clustered", src(storedRange("s1", "A2:A13")),
            {series("A", storedRange("s1", "B2:B13")), series("B", storedRange("s1", "C2:C13"))});
        const auto out = ChartSpec::withType(spec, "pie");
        QCOMPARE(out["type"].toString(), QString("pie"));
        QCOMPARE(out["series"].toArray().size(), 2);
        QVERIFY(out["grouping"].isNull());
    }
    void sourceTextShowsNames() {
        const QHash<QString, QString> names{{"s1", "Sheet1"}, {"s2", "Q1 Sales"}, {"s3", "It's"}};
        QCOMPARE(ChartSpec::sourceText(storedRange("s1", "A2:A13"), names), QString("Sheet1!A2:A13"));
        QCOMPARE(ChartSpec::sourceText(storedRange("s2", "A2:A13"), names), QString("'Q1 Sales'!A2:A13"));
        QCOMPARE(ChartSpec::sourceText(storedRange("s3", "B1"), names), QString("'It''s'!B1"));
        QCOMPARE(ChartSpec::sourceText(storedTable(), names), QString("Orders.Total"));
        QCOMPARE(ChartSpec::sourceText(storedQuery(), names), QString("ByRegion.Sum"));
    }
    void rangeSourceUsesA1() {
        QCOMPARE(ChartSpec::rangeSource("s1", 2, 1, 13, 1), (QJsonObject{{"kind", "range"}, {"sheet", "s1"}, {"ref", "A2:A13"}}));
        QCOMPARE(ChartSpec::rangeSource("s1", 1, 2, 1, 2)["ref"].toString(), QString("B1"));
        QCOMPARE(ChartSpec::rangeSource("s1", 1, 27, 3, 28)["ref"].toString(), QString("AA1:AB3"));
    }
    void rangeTextRoundTrips_data() {
        QTest::addColumn<QString>("text");
        QTest::addColumn<bool>("ok");
        QTest::addColumn<QString>("sheet");
        QTest::addColumn<QString>("ref");
        QTest::newRow("cell") << "A1" << true << "cur" << "A1";
        QTest::newRow("range") << "A2:A13" << true << "cur" << "A2:A13";
        QTest::newRow("named") << "Sheet1!A2:A13" << true << "s1" << "A2:A13";
        QTest::newRow("quoted") << "'Q1 Sales'!$A$2:$A$13" << true << "s2" << "A2:A13";
        QTest::newRow("doubled quote") << "'It''s'!b3" << true << "s3" << "B3";
        QTest::newRow("dollars") << "$B$1" << true << "cur" << "B1";
        QTest::newRow("reversed") << "B5:A2" << true << "cur" << "A2:B5";
        QTest::newRow("unknown sheet") << "Nope!A1" << false << "" << "";
        QTest::newRow("multi area") << "A1:A2,B1:B2" << false << "" << "";
        QTest::newRow("whole column") << "A:A" << false << "" << "";
        QTest::newRow("name") << "Total" << false << "" << "";
        QTest::newRow("empty") << "" << false << "" << "";
        QTest::newRow("row zero") << "A0" << false << "" << "";
    }
    void rangeTextRoundTrips() {
        QFETCH(QString, text);
        QFETCH(bool, ok);
        QFETCH(QString, sheet);
        QFETCH(QString, ref);
        const QHash<QString, QString> ids{{"Sheet1", "s1"}, {"Q1 Sales", "s2"}, {"It's", "s3"}};
        QJsonObject source;
        QCOMPARE(ChartSpec::parseRange(text, ids, "cur", &source), ok);
        if (!ok) return;
        QCOMPARE(source, (QJsonObject{{"kind", "range"}, {"sheet", sheet}, {"ref", ref}}));
        const QHash<QString, QString> names{{"s1", "Sheet1"}, {"s2", "Q1 Sales"}, {"s3", "It's"}, {"cur", "Cur"}};
        QJsonObject stored{{"kind", "range"}, {"sheet_id", sheet}, {"ref", ref}};
        QJsonObject again;
        auto allIds=ids;allIds.insert("Cur","cur");
        QVERIFY(ChartSpec::parseRange(ChartSpec::sourceText(stored, names), allIds, "other", &again));
        QCOMPARE(again, source);
    }
    void resizeHoldsTheOppositeEdge_data() {
        QTest::addColumn<int>("handle");
        QTest::addColumn<QPoint>("delta");
        QTest::addColumn<QRect>("expected");
        // Start 100,100 size 200x150: right 300, bottom 250.
        QTest::newRow("0 top-left") << 0 << QPoint(10, 20) << QRect(QPoint(110, 120), QPoint(299, 249));
        QTest::newRow("1 top") << 1 << QPoint(10, 20) << QRect(QPoint(100, 120), QPoint(299, 249));
        QTest::newRow("2 top-right") << 2 << QPoint(10, 20) << QRect(QPoint(100, 120), QPoint(309, 249));
        QTest::newRow("3 right") << 3 << QPoint(10, 20) << QRect(QPoint(100, 100), QPoint(309, 249));
        QTest::newRow("4 bottom-right") << 4 << QPoint(10, 20) << QRect(QPoint(100, 100), QPoint(309, 269));
        QTest::newRow("5 bottom") << 5 << QPoint(10, 20) << QRect(QPoint(100, 100), QPoint(299, 269));
        QTest::newRow("6 bottom-left") << 6 << QPoint(10, 20) << QRect(QPoint(110, 100), QPoint(299, 269));
        QTest::newRow("7 left") << 7 << QPoint(10, 20) << QRect(QPoint(110, 100), QPoint(299, 249));
        QTest::newRow("0 past minimum") << 0 << QPoint(500, 500) << QRect(236, 202, 64, 48);
        QTest::newRow("2 past minimum") << 2 << QPoint(-500, 500) << QRect(100, 202, 64, 48);
        QTest::newRow("4 past minimum") << 4 << QPoint(-500, -500) << QRect(100, 100, 64, 48);
        QTest::newRow("6 past minimum") << 6 << QPoint(500, -500) << QRect(236, 100, 64, 48);
        QTest::newRow("3 past minimum") << 3 << QPoint(-500, 0) << QRect(100, 100, 64, 150);
        QTest::newRow("7 past minimum") << 7 << QPoint(500, 0) << QRect(236, 100, 64, 150);
        QTest::newRow("1 past minimum") << 1 << QPoint(0, 500) << QRect(100, 202, 200, 48);
        QTest::newRow("5 past minimum") << 5 << QPoint(0, -500) << QRect(100, 100, 200, 48);
        QTest::newRow("4 past maximum") << 4 << QPoint(9000, 9000) << QRect(100, 100, 4096, 4096);
    }
    void resizeHoldsTheOppositeEdge() {
        QFETCH(int, handle);
        QFETCH(QPoint, delta);
        QFETCH(QRect, expected);
        QCOMPARE(ChartSpec::resized(QRect(100, 100, 200, 150), handle, delta), expected);
    }
    void clampSizeKeepsBounds() {
        QCOMPARE(ChartSpec::clampSize(QSize(1, 1)), QSize(64, 48));
        QCOMPARE(ChartSpec::clampSize(QSize(9000, 9000)), QSize(4096, 4096));
        QCOMPARE(ChartSpec::clampSize(QSize(300, 200)), QSize(300, 200));
    }

// A new workbook in a shown window with the chart layer, grid and model at
// hand (as CHART_WINDOW in test_desktop.cpp).
#define CHART_WINDOW \
    QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini"); \
    MainWindow window(options);window.resize(1200,800);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000); \
    QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("charts.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000); \
    auto *layer=window.findChild<ChartLayer *>("chartLayer");QVERIFY(layer);auto *grid=window.findChild<QTableView *>("grid");QVERIFY(grid);auto *model=window.model();Q_UNUSED(model)
    void clickSelectsAChart() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Picked",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=overlayNamed(layer,"Picked");QVERIFY(overlay);
        QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();QSignalSpy changed(layer,&ChartLayer::selectionChanged);QSignalSpy edit(layer,&ChartLayer::editRequested);
        QVERIFY(!overlay->selected());QVERIFY(!highlighted(overlay,QPoint(overlay->width()/4,0)));
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(40,40));
        QCOMPARE(layer->selectedChart(),id);QVERIFY(overlay->selected());QCOMPARE(window.focusWidget(),overlay);
        QCOMPARE(changed.count(),1);QCOMPARE(changed[0][0].toString(),id);
        QCOMPARE(layer->overlay(id),overlay);QCOMPARE(layer->chart(id)["name"].toString(),QString("Picked"));
        // The cell under the chart is not selected by the click.
        QVERIFY(grid->currentIndex()!=model->index(3,4));
        // Decision 4: a 2 px frame and eight 8 px handles inside the rectangle.
        const int w=overlay->width(),h=overlay->height();
        for(const QPoint &p:{QPoint(w/4,0),QPoint(w/4,1),QPoint(0,h/4),QPoint(w-1,h/4),QPoint(w/4,h-1),QPoint(w/4,h-2)})QVERIFY2(highlighted(overlay,p),qPrintable(QString("%1,%2").arg(p.x()).arg(p.y())));
        QVERIFY(!highlighted(overlay,QPoint(w/4,2)));
        for(const QPoint &p:{QPoint(7,7),QPoint(w/2,7),QPoint(w-8,7),QPoint(w-8,h/2),QPoint(w-8,h-8),QPoint(w/2,h-8),QPoint(7,h-8),QPoint(7,h/2)})QVERIFY2(highlighted(overlay,p),qPrintable(QString("handle %1,%2").arg(p.x()).arg(p.y())));
        QVERIFY(!highlighted(overlay,QPoint(9,9)));
        // Enter and a double-click ask for the chart to be edited.
        QTest::keyClick(overlay,Qt::Key_Return);QCOMPARE(edit.count(),1);QCOMPARE(edit[0][0].toString(),id);
        QTest::keyClick(overlay,Qt::Key_Enter);QCOMPARE(edit.count(),2);
        QTest::mouseDClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(40,40));QCOMPARE(edit.count(),3);QCOMPARE(layer->selectedChart(),id);
        QCOMPARE(changed.count(),1);
    }
    void clickOnTheGridDeselects() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Picked",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(layer->selectedChart(),overlay->chartId());
        const QPoint cell(grid->columnViewportPosition(1)+5,grid->rowViewportPosition(1)+5);
        QVERIFY(!overlay->geometry().contains(grid->viewport()->mapTo(overlay->parentWidget(),cell)));
        QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,cell);
        QCOMPARE(layer->selectedChart(),QString());QVERIFY(!overlay->selected());
        QCOMPARE(changed.count(),2);QCOMPARE(changed[1][0].toString(),QString());
        QCOMPARE(grid->currentIndex(),model->index(1,1));QCOMPARE(window.focusWidget(),grid);
        QVERIFY(!highlighted(overlay,QPoint(overlay->width()/4,0)));
    }
    void escapeDeselectsAndFocusesTheGrid() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Picked",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        grid->setCurrentIndex(model->index(5,1));
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(window.focusWidget(),overlay);
        // The overlay claims Escape before any window shortcut.
        QKeyEvent override(QEvent::ShortcutOverride,Qt::Key_Escape,Qt::NoModifier);override.ignore();
        QCoreApplication::sendEvent(overlay,&override);QVERIFY(override.isAccepted());
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        QTest::keyClick(overlay,Qt::Key_Escape);
        QCOMPARE(layer->selectedChart(),QString());QVERIFY(!overlay->selected());QCOMPARE(changed.count(),1);QCOMPARE(changed[0][0].toString(),QString());
        QCOMPARE(window.focusWidget(),grid);QCOMPARE(grid->currentIndex(),model->index(5,1));
        // Not interactive: a click still selects, Escape still deselects, Enter is not taken.
        layer->setInteractive(false);QSignalSpy edit(layer,&ChartLayer::editRequested);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(layer->selectedChart(),overlay->chartId());QCOMPARE(window.focusWidget(),overlay);
        QKeyEvent enter(QEvent::ShortcutOverride,Qt::Key_Return,Qt::NoModifier);enter.ignore();
        QCoreApplication::sendEvent(overlay,&enter);QVERIFY(!enter.isAccepted());
        QTest::keyClick(overlay,Qt::Key_Return);QCOMPARE(edit.count(),0);
        QTest::keyClick(overlay,Qt::Key_Escape);QCOMPARE(layer->selectedChart(),QString());QCOMPARE(window.focusWidget(),grid);
        layer->setInteractive(true);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QTest::keyClick(overlay,Qt::Key_Return);QCOMPARE(edit.count(),1);
    }
    void onlyOneChartIsSelected() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Left",2,2,0,0,200,150));QVERIFY(addChart(window,opened,"Right",2,8,0,0,200,150));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        auto *left=overlayNamed(layer,"Left"),*right=overlayNamed(layer,"Right");QVERIFY(left && right);
        QTRY_VERIFY_WITH_TIMEOUT(left->isVisible() && right->isVisible(),5000);QVERIFY(!left->geometry().intersects(right->geometry()));
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        QTest::mouseClick(left,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QVERIFY(left->selected());QVERIFY(!right->selected());
        QTest::mouseClick(right,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QVERIFY(!left->selected());QVERIFY(right->selected());
        QCOMPARE(layer->selectedChart(),right->chartId());QCOMPARE(window.focusWidget(),right);
        layer->select(left->chartId());QVERIFY(left->selected());QVERIFY(!right->selected());
        // Selecting what is selected changes nothing; an unknown id clears.
        layer->select(left->chartId());QCOMPARE(changed.count(),3);
        layer->select("no-such-chart");QCOMPARE(layer->selectedChart(),QString());QVERIFY(!left->selected() && !right->selected());
        QCOMPARE(changed.count(),4);QCOMPARE(changed[3][0].toString(),QString());
        layer->select({});QCOMPARE(changed.count(),4);
        QVERIFY(layer->overlay("no-such-chart")==nullptr);QVERIFY(layer->chart("no-such-chart").isEmpty());
    }
    void selectionSurvivesARelist() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Kept",2,4));QVERIFY(addChart(window,opened,"Other",12,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);auto *kept=overlayNamed(layer,"Kept");QTRY_VERIFY_WITH_TIMEOUT(kept->isVisible(),5000);
        const auto id=kept->chartId();layer->select(id);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);QSignalSpy listed(layer,&ChartLayer::listed);
        // A change of the chart itself and a change elsewhere both list again.
        window.submit({{"action","chart_update"},{"chart_id",overlayNamed(layer,"Other")->chartId()},{"name","Renamed"}});
        QTRY_VERIFY_WITH_TIMEOUT(overlayNamed(layer,"Renamed"),10000);
        window.submit({{"action","chart_update"},{"chart_id",id},{"offset",QJsonObject{{"x",12},{"y",6}}}});
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["offset"].toObject()["x"].toInt(),12,10000);
        QVERIFY(listed.count()>=2);
        QCOMPARE(layer->selectedChart(),id);QVERIFY(layer->overlay(id)->selected());QVERIFY(!overlayNamed(layer,"Renamed")->selected());
        QCOMPARE(changed.count(),0);
    }
    void selectionClearsWhenTheChartIsGone() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Undone",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(window.focusWidget(),overlay);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        // The signal comes once the chart's overlay is gone.
        int overlaysAtSignal=-1;connect(layer,&ChartLayer::selectionChanged,this,[&]{overlaysAtSignal=int(layer->overlays().size());});
        window.submit({{"action","undo"}});
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,10000);
        QCOMPARE(layer->selectedChart(),QString());QCOMPARE(changed.count(),1);QCOMPARE(changed[0][0].toString(),QString());
        QCOMPARE(overlaysAtSignal,0);
        // Focus does not vanish with the chart: it returns to the grid.
        QCOMPARE(window.focusWidget(),grid);
    }
    void selectionClearsOnSheetSwitch() {
        CHART_WINDOW;window.submit({{"action","sheet_add"},{"name","Other"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(addChart(window,opened,"First",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();layer->select(id);QCOMPARE(layer->selectedChart(),id);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        auto *tabs=window.findChild<QTabBar *>("sheets");tabs->setCurrentIndex(1);
        QCOMPARE(layer->selectedChart(),QString());QCOMPARE(changed.count(),1);QCOMPARE(changed[0][0].toString(),QString());
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,5000);
        // Coming back does not select it again.
        tabs->setCurrentIndex(0);QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        QCOMPARE(layer->selectedChart(),QString());QVERIFY(!layer->overlays().first()->selected());QCOMPARE(changed.count(),1);
    }
    void wheelOverAChartScrollsTheGrid() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Wheel",2,2,0,0,400,300));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        auto *bar=grid->verticalScrollBar();QVERIFY(bar->maximum()>0);const int before=bar->value();
        const QPoint at(30,30);
        QWheelEvent wheel(QPointF(at),QPointF(overlay->mapToGlobal(at)),QPoint(),QPoint(0,-120),Qt::NoButton,Qt::NoModifier,Qt::NoScrollPhase,false);
        QCoreApplication::sendEvent(overlay,&wheel);
        QVERIFY(bar->value()>before);
    }
    void selectNextWrapsInZOrder() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"A",2,2));QVERIFY(addChart(window,opened,"B",4,4));QVERIFY(addChart(window,opened,"C",6,6));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),3,5000);
        const auto a=overlayNamed(layer,"A")->chartId(),b=overlayNamed(layer,"B")->chartId(),c=overlayNamed(layer,"C")->chartId();
        // Put A on top: the z order is then B, C, A.
        window.submit({{"action","chart_update"},{"chart_id",a},{"z",50}});
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(a)["z"].toInt(),50,10000);
        QCOMPARE(layer->selectedChart(),QString());
        layer->selectNext(1);QCOMPARE(layer->selectedChart(),b);QCOMPARE(window.focusWidget(),layer->overlay(b));
        layer->selectNext(1);QCOMPARE(layer->selectedChart(),c);
        layer->selectNext(1);QCOMPARE(layer->selectedChart(),a);
        layer->selectNext(1);QCOMPARE(layer->selectedChart(),b);
        layer->selectNext(-1);QCOMPARE(layer->selectedChart(),a);
        layer->selectNext(-1);QCOMPARE(layer->selectedChart(),c);
        layer->select({});layer->selectNext(-1);QCOMPARE(layer->selectedChart(),a);
        QVERIFY(layer->overlay(a)->selected());QVERIFY(!layer->overlay(b)->selected() && !layer->overlay(c)->selected());
    }
    void overlayIsAccessible() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Plain",2,2));
        auto spec=chartSpec("Sheet1");spec["type"]="line";spec["title"]=QJsonObject{{"text","Units by month"}};
        auto list=spec["series"].toArray();list.append(list.first());spec["series"]=list;
        QVERIFY(addChart(window,opened,"Titled",12,2,0,0,320,200,"Sheet1",spec));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        auto *plain=overlayNamed(layer,"Plain"),*titled=overlayNamed(layer,"Titled");QVERIFY(plain && titled);
        QCOMPARE(plain->accessibleName(),QString("Chart: Plain"));QCOMPARE(plain->accessibleDescription(),QString("Column chart, 1 series"));
        QCOMPARE(titled->accessibleName(),QString("Chart: Titled"));QCOMPARE(titled->accessibleDescription(),QString("Line chart, titled Units by month, 2 series"));
        QCOMPARE(plain->focusPolicy(),Qt::ClickFocus);QVERIFY(!plain->testAttribute(Qt::WA_TransparentForMouseEvents));
        auto *iface=QAccessible::queryAccessibleInterface(titled);QVERIFY(iface);
        QCOMPARE(iface->text(QAccessible::Name),QString("Chart: Titled"));
        QCOMPARE(iface->text(QAccessible::Description),QString("Line chart, titled Units by month, 2 series"));
        QVERIFY(iface->state().focusable);
        // A rename is read again with the catalog.
        window.submit({{"action","chart_update"},{"chart_id",plain->chartId()},{"name","Renamed"}});
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(plain->chartId())->accessibleName(),QString("Chart: Renamed"),10000);
    }
    void brokenChartCanBeSelected() {
        CHART_WINDOW;window.submit({{"action","sheet_add"},{"name","Data"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(addChart(window,opened,"Broken chart",2,3,0,0,320,200,"Data"));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        auto *tabs=window.findChild<QTabBar *>("sheets");tabs->setCurrentIndex(1);QCOMPARE(tabs->tabText(1),QString("Data"));
        const int before=opened.count();window.submit({{"action","sheet_delete"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);
        QTRY_VERIFY_WITH_TIMEOUT(overlayNamed(layer,"Broken chart") && overlayNamed(layer,"Broken chart")->placeholder().contains("source"),5000);
        auto *broken=overlayNamed(layer,"Broken chart");QTRY_VERIFY_WITH_TIMEOUT(broken->isVisible(),5000);
        const auto reason=ChartSpec::stateReason(layer->chart(broken->chartId()));QVERIFY(!reason.isEmpty());
        QCOMPARE(broken->accessibleDescription(),reason);QCOMPARE(broken->accessibleName(),QString("Chart: Broken chart"));
        QTest::mouseClick(broken,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));
        QCOMPARE(layer->selectedChart(),broken->chartId());QVERIFY(broken->selected());QCOMPARE(window.focusWidget(),broken);
        QVERIFY(highlighted(broken,QPoint(broken->width()/4,0)));QVERIFY(highlighted(broken,QPoint(7,7)));
    }
    void headerPressDeselectsAndFocusesTheGrid() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Picked",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(window.focusWidget(),overlay);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        auto *header=grid->horizontalHeader();
        QTest::mouseClick(header->viewport(),Qt::LeftButton,Qt::NoModifier,QPoint(header->sectionViewportPosition(1)+5,header->height()/2));
        QCOMPARE(layer->selectedChart(),QString());QVERIFY(!overlay->selected());QCOMPARE(changed.count(),1);QCOMPARE(changed[0][0].toString(),QString());
        QCOMPARE(window.focusWidget(),grid);
        // The layer itself returns the focus, not only Qt's click focus: a
        // right-button press sent straight to the header (no click focus, and
        // the header ignores it) still deselects and gives the grid the focus.
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(window.focusWidget(),overlay);
        const QPointF at(header->sectionViewportPosition(1)+5,header->height()/2);
        QMouseEvent press(QEvent::MouseButtonPress,at,header->viewport()->mapToGlobal(at),Qt::RightButton,Qt::RightButton,Qt::NoModifier);
        QCoreApplication::sendEvent(header->viewport(),&press);
        QCOMPARE(layer->selectedChart(),QString());QCOMPARE(changed.count(),3);QCOMPARE(window.focusWidget(),grid);
    }
    void enterOnAnUnselectedChartDoesNothing() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Picked",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        QCOMPARE(overlay->focusPolicy(),Qt::ClickFocus);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));layer->select({});
        QCOMPARE(window.focusWidget(),overlay);QVERIFY(!overlay->selected());
        QSignalSpy edit(layer,&ChartLayer::editRequested);
        QKeyEvent enter(QEvent::ShortcutOverride,Qt::Key_Return,Qt::NoModifier);enter.ignore();
        QCoreApplication::sendEvent(overlay,&enter);QVERIFY(!enter.isAccepted());
        QTest::keyClick(overlay,Qt::Key_Return);QTest::keyClick(overlay,Qt::Key_Enter);QCOMPARE(edit.count(),0);
        // A focused chart that goes, selected or not, gives the grid the focus.
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,10000);
        QCOMPARE(window.focusWidget(),grid);
    }
    void projectedViewDeselects() {
        CHART_WINDOW;
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",1},{"source","1"}},QJsonObject{{"row",3},{"col",1},{"source","2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(addChart(window,opened,"Listed",2,3));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(window.focusWidget(),overlay);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);const int before=opened.count();
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false},{"sort",QJsonArray{QJsonObject{{"col",1}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);QVERIFY(model->projected());
        QCOMPARE(layer->selectedChart(),QString());QCOMPARE(changed.count(),1);QCOMPARE(changed[0][0].toString(),QString());
        QVERIFY(window.focusWidget()!=overlay);QVERIFY(!qobject_cast<ChartOverlay *>(window.focusWidget()));
    }
    void formatPainterClicksThroughCharts() {
        CHART_WINDOW;const QJsonObject bold{{"bold",true}};
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","x"},{"style",bold}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(addChart(window,opened,"Covering",3,3,0,0,400,300));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        auto *area=overlay->parentWidget();const QPoint point(grid->columnViewportPosition(3)+5,grid->rowViewportPosition(3)+5);
        QCOMPARE(qobject_cast<ChartOverlay *>(area->childAt(grid->viewport()->mapTo(area,point))),overlay);
        grid->setCurrentIndex(model->index(0,0));grid->selectionModel()->select(model->index(0,0),QItemSelectionModel::ClearAndSelect);
        auto *painter=window.findChild<QAction *>("formatPainterAction");painter->trigger();QTRY_VERIFY_WITH_TIMEOUT(painter->property("ready").toBool(),5000);
        QVERIFY(overlay->testAttribute(Qt::WA_TransparentForMouseEvents));QCOMPARE(overlay->focusPolicy(),Qt::NoFocus);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);const int before=opened.count();
        windowClick(grid->viewport(),point);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(3,3)),5000);
        QCOMPARE(model->cell(model->index(3,3))["style"].toObject(),bold);
        QCOMPARE(changed.count(),0);QCOMPARE(layer->selectedChart(),QString());
        // The painter is done: the chart takes clicks again.
        QVERIFY(!painter->isChecked());
        auto *again=layer->overlays().first();QVERIFY(!again->testAttribute(Qt::WA_TransparentForMouseEvents));QCOMPARE(again->focusPolicy(),Qt::ClickFocus);
    }
    void referencePickClicksThroughCharts() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Covering",3,3,0,0,400,300));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        auto *source=window.findChild<QLineEdit *>("source");auto *pick=window.findChild<QAction *>("pickReferenceAction");QVERIFY(source && pick);
        grid->setCurrentIndex(model->index(0,0));QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus();QTest::keyClicks(source,"=SUM(");pick->trigger();QVERIFY(pick->isChecked());
        QVERIFY(overlay->testAttribute(Qt::WA_TransparentForMouseEvents));
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        const QPoint point(grid->columnViewportPosition(3)+5,grid->rowViewportPosition(3)+5);
        QVERIFY(overlay->geometry().contains(grid->viewport()->mapTo(overlay->parentWidget(),point)));
        windowClick(grid->viewport(),point);
        QCOMPARE(source->text(),QString("=SUM(D4"));QCOMPARE(window.focusWidget(),source);
        QCOMPARE(changed.count(),0);QCOMPARE(grid->currentIndex(),model->index(0,0));
        QTest::keyClick(source,Qt::Key_Escape);
        QTRY_VERIFY_WITH_TIMEOUT(!pick->isChecked(),5000);QVERIFY(!overlay->testAttribute(Qt::WA_TransparentForMouseEvents));
    }
#undef CHART_WINDOW

private:
    // Labels in A2:A4, a series name in B1 and values in B2:B4, as chartSpec reads them.
    static bool fillChartData(MainWindow &window,QSignalSpy &opened) {
        const auto before=opened.count();QJsonArray cells{QJsonObject{{"row",1},{"col",2},{"source","Sales"}}};
        for(int row=2;row<=4;++row){cells.append(QJsonObject{{"row",row},{"col",1},{"source","Item "+QString::number(row)}});cells.append(QJsonObject{{"row",row},{"col",2},{"source",QString::number(row*100)}});}
        window.submit({{"action","edit"},{"cells",cells}});
        for(int i=0;i<1000 && opened.count()==before;++i)QTest::qWait(10);
        return opened.count()>before;
    }
    static QJsonObject chartSpec(const QString &sheet) {
        auto range=[sheet](const QString &ref){return QJsonObject{{"source",QJsonObject{{"kind","range"},{"sheet",sheet},{"ref",ref}}}};};
        return {{"spec_version",1},{"type","column"},{"categories",range("A2:A4")},{"series",QJsonArray{QJsonObject{{"name",range("B1")},{"values",range("B2:B4")}}}}};
    }
    // Creates a chart on the current sheet through the service and waits for
    // the window to accept the new state.
    static bool addChart(MainWindow &window,QSignalSpy &opened,const QString &name,int row,int col,int x=0,int y=0,int width=320,int height=200,
                         const QString &source="Sheet1",const QJsonObject &spec={}) {
        const auto before=opened.count();
        window.submit({{"action","chart_create"},{"name",name},{"anchor",QJsonObject{{"row",row},{"col",col}}},{"offset",QJsonObject{{"x",x},{"y",y}}},
                       {"size",QJsonObject{{"width",width},{"height",height}}},{"spec",spec.isEmpty()?chartSpec(source):spec}});
        for(int i=0;i<1000 && opened.count()==before;++i)QTest::qWait(10);
        return opened.count()>before;
    }
    static ChartOverlay *overlayNamed(ChartLayer *layer,const QString &name) {
        for(auto *overlay:layer->overlays())if(overlay->chart()["name"].toString()==name)return overlay;
        return nullptr;
    }
    // A click delivered through the window, so the widget under the point
    // (an overlay or the cells) receives it as a real click would.
    static void windowClick(QWidget *widget,const QPoint &point) {
        auto *top=widget->window();QTest::mouseClick(top->windowHandle(),Qt::LeftButton,Qt::NoModifier,widget->mapTo(top,point));
    }
    // True when the overlay's own painting is the highlight colour at point.
    static bool highlighted(QWidget *widget,const QPoint &point) {
        return widget->grab().toImage().pixelColor(point*widget->devicePixelRatioF()).rgb()==widget->palette().color(QPalette::Highlight).rgb();
    }
};

QTEST_MAIN(ChartAuthoringTests)
#include "test_chart_authoring.moc"
