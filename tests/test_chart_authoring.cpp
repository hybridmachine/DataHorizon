// SPDX-License-Identifier: GPL-3.0-only
#include "chartspec.h"
#include <QHash>
#include <QJsonArray>
#include <QJsonObject>
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
};

QTEST_MAIN(ChartAuthoringTests)
#include "test_chart_authoring.moc"
