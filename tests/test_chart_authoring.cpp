// SPDX-License-Identifier: GPL-3.0-only
#include "chartlayer.h"
#include "chartpanel.h"
#include "chartspec.h"
#include "mainwindow.h"
#include <QAccessible>
#include <QAction>
#include <QComboBox>
#include <QDockWidget>
#include <QPushButton>
#include <QScrollArea>
#include <QScopeGuard>
#include <QToolButton>
#include <QHash>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QMenu>
#include <QMenuBar>
#include <QPlainTextEdit>
#include <QPointer>
#include <QProcess>
#include <QRegularExpression>
#include <QTcpServer>
#include <QTcpSocket>
#include <functional>
#include "restclient.h"
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

// Forwards every call to a real service. Suggest answers, workbook metadata
// reads, chart catalog reads and operation answers can be held until released, chart_update
// operations can be refused without reaching the service (the refusal held like any
// operation answer), the next chart catalog read can fail, and the answer to the next
// chart_create can be lost (the operation still reaches the service).
class ChartProxy : public QTcpServer {
public:
    bool holdSuggest=false,holdMetadata=false,holdCharts=false,holdOperations=false,loseCreate=false,refuseChartUpdates=false,failCharts=false;
    int suggests=0;
    explicit ChartProxy(const QJsonObject &service) : m_upstream(this) {
        m_upstream.configure(service["url"].toString(),service["token"].toString());
        connect(this,&QTcpServer::newConnection,this,[this]{
            auto *socket=nextPendingConnection();connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket]{
                const auto request=socket->property("request").toByteArray()+socket->readAll();socket->setProperty("request",request);
                const auto end=request.indexOf("\r\n\r\n");if(end<0 || socket->property("handled").toBool())return;
                const int length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                if(request.size()<end+4+length)return;
                socket->setProperty("handled",true);
                const auto path=QString::fromLatin1(request.left(request.indexOf("\r\n")).split(' ').at(1));
                const bool post=request.startsWith("POST ");
                const auto body=QJsonDocument::fromJson(request.mid(end+4)).object();
                const auto parts=path.split('/',Qt::SkipEmptyParts);
                const bool suggest=path.endsWith("/charts/suggest"),metadata=!post && parts.size()==3 && parts[1]=="workbooks",catalog=path.endsWith("/charts");
                const bool operation=post && path.endsWith("/operations");
                const bool lose=loseCreate && path.endsWith("/operations") && body["action"].toString()=="chart_create";
                if(lose)loseCreate=false;
                if(suggest)++suggests;
                const QPointer<QTcpSocket> guarded=socket;
                if(operation && refuseChartUpdates && body["action"].toString()=="chart_update") {
                    std::function<void()> reply=[guarded,id=body["operation_id"]]{send(guarded,{{"operation_id",id},{"outcome","rejected"},{"code","INVALID_ARGUMENT"},{"message","The test proxy refused this chart change"}},"refused");};
                    if(holdOperations)m_operations.append(reply);else reply();
                    return;
                }
                if(catalog && failCharts) {
                    failCharts=false;send(guarded,{{"code","UNAVAILABLE"},{"message","The test proxy failed this chart read"}},"failed");
                    return;
                }
                m_upstream.request(path,body,[this,guarded,suggest,metadata,catalog,operation,lose](QJsonObject result,QString error){
                    if(!guarded)return;
                    if(lose){guarded->abort();return;}
                    std::function<void()> reply=[guarded,result,error]{send(guarded,result,error);};
                    if(operation && holdOperations)m_operations.append(reply);
                    else if(suggest && holdSuggest)m_suggest.append(reply);
                    else if(metadata && holdMetadata)m_metadata.append(reply);
                    else if(catalog && holdCharts)m_charts.append(reply);
                    else reply();
                },post);
            });
        });
    }
    int heldMetadata() const { return int(m_metadata.size()); }
    void releaseSuggest() { holdSuggest=false;for(const auto &reply:std::exchange(m_suggest,{}))reply(); }
    void releaseCharts() { holdCharts=false;for(const auto &reply:std::exchange(m_charts,{}))reply(); }
    void releaseMetadata() { holdMetadata=false;for(const auto &reply:std::exchange(m_metadata,{}))reply(); }
    void releaseOperations() { holdOperations=false;for(const auto &reply:std::exchange(m_operations,{}))reply(); }
private:
    static void send(const QPointer<QTcpSocket> &socket,const QJsonObject &result,const QString &error) {
        if(!socket)return;
        const auto bytes=QJsonDocument(error.isEmpty() || !result.isEmpty()?result:QJsonObject{{"code","UNAVAILABLE"},{"message",error}}).toJson(QJsonDocument::Compact);
        socket->write(QByteArray("HTTP/1.1 ")+(error.isEmpty()?"200 OK":"409 Conflict")+"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(bytes.size())+"\r\n\r\n"+bytes);
        socket->disconnectFromHost();
    }
    RestClient m_upstream;
    QList<std::function<void()>> m_suggest,m_metadata,m_charts,m_operations;
};

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
    void boundsRejectGroupSeparators_data() {
        QTest::addColumn<QString>("locale");
        QTest::addColumn<QString>("text");
        QTest::addColumn<bool>("sent");
        QTest::addColumn<double>("value");
        // Where the comma is the decimal mark, "1,500" is one and a half; where
        // it would only group digits it is refused rather than read as 1500.
        QTest::newRow("de 1,500") << "de_DE" << "1,500" << true << 1.5;
        QTest::newRow("en 1,500") << "en_US" << "1,500" << false << 0.0;
        QTest::newRow("C 1,500") << "C" << "1,500" << false << 0.0;
        QTest::newRow("en 1e3") << "en_US" << "1e3" << true << 1000.0;
        QTest::newRow("de 1e3") << "de_DE" << "1e3" << true << 1000.0;
        QTest::newRow("en spaces") << "en_US" << " 5 " << true << 5.0;
        QTest::newRow("de 2.5") << "de_DE" << "2.5" << true << 2.5;
        QTest::newRow("en abc") << "en_US" << "abc" << false << 0.0;
        QTest::newRow("de abc") << "de_DE" << "abc" << false << 0.0;
    }
    void boundsRejectGroupSeparators() {
        QFETCH(QString,locale);QFETCH(QString,text);QFETCH(bool,sent);QFETCH(double,value);
        const QLocale before;QLocale::setDefault(QLocale(locale));
        auto restore=qScopeGuard([&]{QLocale::setDefault(before);});
        ChartPanel panel;panel.setChart(chartWith(specOf("column",QJsonValue::Null,src(storedRange("s1","A2:A13")),{series("S",storedRange("s1","B2:B13"))})));
        QSignalSpy requested(&panel,&ChartPanel::changeRequested);
        auto *minimum=panel.findChild<QLineEdit *>("valueAxisMin");QTest::keyClicks(minimum,text);QTest::keyClick(minimum,Qt::Key_Return);
        QCOMPARE(requested.count(),sent?1:0);
        if(sent) {
            const auto bound=requested[0][1].toJsonObject()["spec"].toObject()["axes"].toObject()["value"].toObject()["min"];
            QVERIFY(bound.isDouble());QCOMPARE(bound.toDouble(),value);
        } else {
            QCOMPARE(panel.findChild<QLabel *>("valueAxisMinMessage")->text(),QString("Enter a number or leave empty for automatic"));QCOMPARE(minimum->text(),QString());
        }
    }
    void rangeTextIsBareOnTheChartSheet() {
        const QHash<QString, QString> names{{"s1", "Sheet1"}, {"s2", "Q1 Sales"}};
        QCOMPARE(ChartSpec::rangeText(storedRange("s1", "A2:A13"), names, "s1"), QString("A2:A13"));
        QCOMPARE(ChartSpec::rangeText(storedRange("s2", "A2:A13"), names, "s1"), QString("'Q1 Sales'!A2:A13"));
        QCOMPARE(ChartSpec::rangeText(QJsonObject{{"kind", "range"}, {"sheet", "s1"}, {"ref", "B1"}}, names, "s1"), QString("B1"));
        QCOMPARE(ChartSpec::rangeText(QJsonObject{{"kind", "range"}, {"sheet", "s2"}, {"ref", "B1"}}, names, "s1"), QString("'Q1 Sales'!B1"));
        // A broken range (no ref) is drawn from its rectangle.
        auto broken = storedRange("gone", "", true);broken["ref"] = QJsonValue::Null;
        QCOMPARE(ChartSpec::rangeText(broken, names, "s1"), QString("gone!A2:A13"));
        // One whose sheet was deleted keeps no rectangle and shows nothing.
        for (const char *key : {"row1", "col1", "row2", "col2"}) broken[key] = QJsonValue::Null;
        QCOMPARE(ChartSpec::rangeText(broken, names, "s1"), QString());
    }
    void workingSpecKeepsBrokenSourcesStored() {
        const auto broken = storedRange("gone", "A2:A13", true);
        const auto spec = specOf("column", "clustered", src(broken), {series("S", storedRange("s1", "B2:B13")), series("T", storedTable())});
        const auto working = ChartSpec::workingSpec(chartWith(spec));
        QCOMPARE(working["categories"].toObject()["source"].toObject(), broken);
        QCOMPARE(working["series"].toArray()[0].toObject()["values"].toObject()["source"].toObject(), (QJsonObject{{"kind", "range"}, {"sheet", "s1"}, {"ref", "B2:B13"}}));
        QCOMPARE(working["series"].toArray()[1].toObject()["values"].toObject()["source"].toObject(), (QJsonObject{{"kind", "table"}, {"table", "t1"}, {"column", "c1"}}));
        QVERIFY(ChartSpec::holdsBrokenSource(working));
        // Replaced, nothing is broken and it is the input spec.
        auto repaired = working;repaired["categories"] = src(QJsonObject{{"kind", "range"}, {"sheet", "s1"}, {"ref", "A2:A13"}});
        QVERIFY(!ChartSpec::holdsBrokenSource(repaired));
        auto fixed = spec;fixed["categories"] = src(storedRange("s1", "A2:A13"));
        QCOMPARE(repaired, ChartSpec::inputSpec(chartWith(fixed)));
        QCOMPARE(ChartSpec::workingSpec(chartWith(fixed)), ChartSpec::inputSpec(chartWith(fixed)));
        auto unreadable = chartWith(spec);unreadable["issue"] = "INVALID_SPEC";
        QVERIFY(ChartSpec::workingSpec(unreadable).isEmpty());
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
    void passThroughTakesTheKeyboardButKeepsTheSelection() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Picked",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(window.focusWidget(),overlay);QCOMPARE(layer->selectedChart(),id);
        QSignalSpy changed(layer,&ChartLayer::selectionChanged);QSignalSpy edit(layer,&ChartLayer::editRequested);
        layer->setPassThrough(true);
        QCOMPARE(window.focusWidget(),grid);QCOMPARE(layer->selectedChart(),id);QVERIFY(overlay->selected());
        for(const auto key:{Qt::Key_Return,Qt::Key_Enter,Qt::Key_Escape}) {
            QKeyEvent override(QEvent::ShortcutOverride,key,Qt::NoModifier);override.ignore();
            QCoreApplication::sendEvent(overlay,&override);QVERIFY(!override.isAccepted());
            QTest::keyClick(overlay,key);
        }
        QCOMPARE(edit.count(),0);QCOMPARE(changed.count(),0);QCOMPARE(layer->selectedChart(),id);QVERIFY(overlay->selected());
        layer->setPassThrough(false);layer->select({});QCOMPARE(changed.count(),1);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));
        QCOMPARE(layer->selectedChart(),id);QCOMPARE(changed.count(),2);QCOMPARE(window.focusWidget(),overlay);
    }
    void insertMenuSitsBetweenEditAndFormat() {
        CHART_WINDOW;
        auto *menu=window.findChild<QMenu *>("insertMenu");QVERIFY(menu);QCOMPARE(menu->title(),QString("&Insert"));
        QStringList titles;for(auto *action:window.menuBar()->actions())titles.append(action->text());
        QCOMPARE(titles,(QStringList{"&File","&Edit","&Insert","F&ormat","&Sheet","&Data","&Tools","&View","&Help"}));
        const auto actions=menu->actions();QCOMPARE(actions.size(),4);
        QCOMPARE(actions[0]->objectName(),QString("insertChartAction"));QCOMPARE(actions[0]->text(),QString("Chart"));QCOMPARE(actions[0]->shortcut(),QKeySequence("Alt+F1"));
        QVERIFY(actions[1]->isSeparator());
        QCOMPARE(actions[2]->objectName(),QString("selectNextChartAction"));QCOMPARE(actions[2]->text(),QString("Select Next Chart"));QCOMPARE(actions[2]->shortcut(),QKeySequence("Ctrl+Alt+C"));
        QCOMPARE(actions[3]->objectName(),QString("selectPreviousChartAction"));QCOMPARE(actions[3]->text(),QString("Select Previous Chart"));QCOMPARE(actions[3]->shortcut(),QKeySequence("Ctrl+Alt+Shift+C"));
        // A workbook with no charts: Chart works, the Select commands do not.
        QVERIFY(actions[0]->isEnabled());QVERIFY(!actions[2]->isEnabled());QVERIFY(!actions[3]->isEnabled());
        QVERIFY(addChart(window,opened,"One",2,4));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        QTRY_VERIFY_WITH_TIMEOUT(actions[2]->isEnabled() && actions[3]->isEnabled(),5000);
        actions[2]->trigger();QCOMPARE(layer->selectedChart(),layer->overlays().first()->chartId());QCOMPARE(window.focusWidget(),layer->overlays().first());
        layer->select({});actions[3]->trigger();QCOMPARE(layer->selectedChart(),layer->overlays().first()->chartId());
    }
    void insertChartCreatesASelectedChart() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        selectCells(grid,model,0,0,3,1);
        auto *insert=window.findChild<QAction *>("insertChartAction");QVERIFY(insert && insert->isEnabled());
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        insert->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,10000);
        QTRY_VERIFY_WITH_TIMEOUT(!layer->selectedChart().isEmpty(),10000);
        const auto id=layer->selectedChart();QCOMPARE(id,layer->overlays().first()->chartId());QVERIFY(layer->overlay(id)->selected());
        QCOMPARE(changed.count(),1);QCOMPARE(admitted.count(),1);
        const auto chart=layer->chart(id);
        QCOMPARE(chart["anchor"].toObject()["row"].toInt(),1);QCOMPARE(chart["anchor"].toObject()["col"].toInt(),3);
        QCOMPARE(chart["name"].toString(),QString("Chart 1"));
        QCOMPARE(chart["size"].toObject()["width"].toInt(),480);QCOMPARE(chart["size"].toObject()["height"].toInt(),300);
        QCOMPARE(chart["spec"].toObject()["series"].toArray().size(),1);
        QCOMPARE(statusText(window),QString("Chart inserted • Undo removes it"));
        QVERIFY(issuesText(window).isEmpty());
    }
    void oneUndoRemovesTheInsertedChart() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        selectCells(grid,model,0,0,3,1);
        window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(!layer->selectedChart().isEmpty(),10000);
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(1,1)),5000);QCOMPARE(model->cell(model->index(1,1))["source"].toString(),QString("200"));
        auto *undo=window.findChild<QAction *>("undoAction");QTRY_VERIFY_WITH_TIMEOUT(undo->isEnabled(),5000);
        const int before=opened.count();undo->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,10000);QCOMPARE(layer->selectedChart(),QString());
        // Only the chart went: the cells are as they were.
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(1,1)),5000);QCOMPARE(model->cell(model->index(1,1))["source"].toString(),QString("200"));
    }
    void insertWithoutNumericDataShowsTheReason() {
        CHART_WINDOW;
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","a"}},QJsonObject{{"row",2},{"col",1},{"source","b"}},QJsonObject{{"row",1},{"col",2},{"source","c"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        selectCells(grid,model,0,0,1,1);
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(issuesText(window).contains("no numeric data"),10000);
        QTest::qWait(200);QCOMPARE(admitted.count(),0);QCOMPARE(layer->overlays().size(),0);QCOMPARE(layer->selectedChart(),QString());
    }
    void insertAnchorsOnTheSelectionWhenNoColumnIsToTheRight() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        // Hide column C, the one to the right of A1:B4.
        selectCells(grid,model,0,2,0,2);window.findChild<QAction *>("hideColumnsAction")->trigger();QVERIFY(grid->isColumnHidden(2));
        selectCells(grid,model,0,0,3,1);
        window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(!layer->selectedChart().isEmpty(),10000);
        const auto anchor=layer->chart(layer->selectedChart())["anchor"].toObject();
        QCOMPARE(anchor["row"].toInt(),1);QCOMPARE(anchor["col"].toInt(),1);
    }
    void insertIsRefusedOverHiddenRows() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        selectCells(grid,model,2,0,2,0);window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY(grid->isRowHidden(2));
        selectCells(grid,model,0,0,3,1);
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        window.findChild<QAction *>("insertChartAction")->trigger();
        QVERIFY(issuesText(window).contains("Show hidden rows"));
        QTest::qWait(300);QCOMPARE(admitted.count(),0);QCOMPARE(layer->overlays().size(),0);
    }
    void insertIsDisabled_data() {
        QTest::addColumn<QString>("mode");
        QTest::newRow("no workbook") << "no workbook";
        QTest::newRow("read-only") << "read-only";
        QTest::newRow("projected view") << "projected view";
        QTest::newRow("relational workbench") << "relational workbench";
        QTest::newRow("busy") << "busy";
    }
    void insertIsDisabled() {
        QFETCH(QString,mode);
        if(mode=="no workbook") {
            QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            for(const char *name:{"insertChartAction","selectNextChartAction","selectPreviousChartAction"})QVERIFY2(!window.findChild<QAction *>(name)->isEnabled(),name);
            return;
        }
        CHART_WINDOW;
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",1},{"source","1"}},QJsonObject{{"row",3},{"col",1},{"source","2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(addChart(window,opened,"Existing",2,4));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        auto *insert=window.findChild<QAction *>("insertChartAction"),*next=window.findChild<QAction *>("selectNextChartAction"),*previous=window.findChild<QAction *>("selectPreviousChartAction");
        QTRY_VERIFY_WITH_TIMEOUT(insert->isEnabled() && next->isEnabled() && previous->isEnabled(),5000);
        bool selectable=false;
        if(mode=="read-only") {
            const int before=opened.count();window.openBook(temp.filePath("charts.dhwb"),false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);
            QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);selectable=true;
        } else if(mode=="projected view") {
            const int before=opened.count();window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false},{"sort",QJsonArray{QJsonObject{{"col",1}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);QVERIFY(model->projected());
        } else if(mode=="busy") {
            // An operation in flight; the checks below run before it answers.
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",6},{"col",1},{"source","9"}}}}});selectable=true;
        } else {
            window.findChild<QAction *>("relationalWorkbenchAction")->trigger();
        }
        QTRY_VERIFY_WITH_TIMEOUT(!insert->isEnabled(),5000);
        QCOMPARE(next->isEnabled(),selectable);QCOMPARE(previous->isEnabled(),selectable);
        // Asked directly, Insert does nothing either.
        selectCells(grid,model,0,0,2,0);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        window.insertChart();QTest::qWait(300);QCOMPARE(admitted.count(),0);
        if(selectable){next->trigger();QCOMPARE(layer->selectedChart(),layer->overlays().first()->chartId());}
    }
    void insertAtTheChartLimitShowsTheReason() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        for(int i=0;i<64;++i)QVERIFY2(addChart(window,opened,QString("C%1").arg(i),10+i,8,0,0,64,48),qPrintable(QString::number(i)));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),64,10000);
        selectCells(grid,model,0,0,3,1);
        QSignalSpy finished(&window,&MainWindow::operationFinished);
        window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(finished.count(),1,10000);QCOMPARE(finished[0][0].toBool(),false);
        QVERIFY2(issuesText(window).contains("at most 64 charts"),qPrintable(issuesText(window)));
        QTest::qWait(300);QCOMPARE(layer->overlays().size(),64);QCOMPARE(layer->selectedChart(),QString());
    }
    void insertAnchorsOnTheSelectionInTheLastColumn() {
        CHART_WINDOW;const int last=model->columnCount();QVERIFY(last>2);
        QJsonArray cells;for(int row=1;row<=3;++row){cells.append(QJsonObject{{"row",row},{"col",last-1},{"source","Item "+QString::number(row)}});cells.append(QJsonObject{{"row",row},{"col",last},{"source",QString::number(row*10)}});}
        window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        selectCells(grid,model,0,last-2,2,last-1);
        window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(!layer->selectedChart().isEmpty(),10000);
        const auto anchor=layer->chart(layer->selectedChart())["anchor"].toObject();
        QCOMPARE(anchor["row"].toInt(),1);QCOMPARE(anchor["col"].toInt(),last-1);
    }
    void dragMovesAChartWithOneUpdate() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Dragged",2,2,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();auto *area=overlay->parentWidget();
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy moved(layer,&ChartLayer::moveRequested);
        const QPoint delta(150,70);
        const QPoint end=dragBy(overlay,QPoint(100,100),delta,false);
        // The chart follows the pointer, drawn whole; nothing is sent before the release.
        QCOMPARE(overlay->geometry(),before.translated(delta));QCOMPARE(overlay->mask(),QRegion(overlay->rect()));
        QCOMPARE(admitted.count(),0);QCOMPARE(layer->selectedChart(),id);QCOMPARE(overlay->cursor().shape(),Qt::SizeAllCursor);
        // placement() is the cell under the top-left corner and the corner's offset inside it.
        QJsonObject anchor,offset;QVERIFY(layer->placement(overlay->geometry(),&anchor,&offset));
        QJsonObject cell,inside;QVERIFY(cellUnder(grid,area,overlay->geometry().topLeft(),&cell,&inside));
        QCOMPARE(anchor,cell);QCOMPARE(offset,inside);QVERIFY(anchor!=(QJsonObject{{"row",2},{"col",2}}));
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QCOMPARE(moved.count(),1);QCOMPARE(admitted.count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject(),anchor,10000);
        QCOMPARE(layer->chart(id)["offset"].toObject(),offset);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->geometry(),before.translated(delta),5000);
        QCOMPARE(layer->selectedChart(),id);
        // The move is one Undo.
        QVERIFY(idle(window));window.findChild<QAction *>("undoAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",2},{"col",2}}),10000);
        QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",0},{"y",0}}));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->geometry(),before,5000);
    }
    void dragIntoAFrozenRowAnchorsThere() {
        CHART_WINDOW;window.findChild<QAction *>("freezeTopRowAction")->trigger();
        auto *top=window.findChild<QTableView *>("frozenTopRow");QVERIFY(top);QTRY_VERIFY_WITH_TIMEOUT(top->isVisible(),5000);
        QVERIFY(addChart(window,opened,"Rising",6,3,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();auto *area=overlay->parentWidget();
        const QPoint target=top->viewport()->mapTo(area,QPoint(top->columnViewportPosition(2)+10,top->rowViewportPosition(0)+5));
        const QPoint end=dragBy(overlay,QPoint(40,40),target-overlay->geometry().topLeft(),false);
        QCOMPARE(overlay->geometry().topLeft(),target);
        // Over the frozen row the dragged chart is still drawn whole.
        QCOMPARE(overlay->mask(),QRegion(overlay->rect()));
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",1},{"col",3}}),10000);
        QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",10},{"y",5}}));
        // Placed from the frozen pane, it stays put while the body scrolls.
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->geometry().topLeft(),target,5000);
        grid->verticalScrollBar()->setValue(grid->verticalScrollBar()->value()+3);
        QCOMPARE(layer->overlay(id)->geometry().topLeft(),target);
    }
    void escapeCancelsADrag() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Kept",2,2,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy changed(layer,&ChartLayer::selectionChanged);
        const QPoint end=dragBy(overlay,QPoint(60,60),QPoint(120,40),false);
        QCOMPARE(overlay->geometry(),before.translated(120,40));
        // Escape during a drag cancels it only: the chart goes back and stays selected.
        QTest::keyClick(overlay,Qt::Key_Escape);
        QCOMPARE(overlay->geometry(),before);QCOMPARE(layer->selectedChart(),id);QVERIFY(overlay->selected());QCOMPARE(window.focusWidget(),overlay);
        // The rest of the gesture does nothing.
        QTest::mouseMove(overlay,overlay->mapFromGlobal(end+QPoint(30,30)));QCOMPARE(overlay->geometry(),before);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end+QPoint(30,30)));QCOMPARE(overlay->geometry(),before);
        QTest::qWait(200);QCOMPARE(admitted.count(),0);QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",0},{"y",0}}));
        QCOMPARE(changed.count(),1);
        // With no drag, Escape deselects as before.
        QTest::keyClick(overlay,Qt::Key_Escape);QCOMPARE(layer->selectedChart(),QString());QCOMPARE(window.focusWidget(),grid);
    }
    void releaseOverAHeaderCancelsTheDrag() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Homing",3,3,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();auto *area=overlay->parentWidget();
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy moved(layer,&ChartLayer::moveRequested);
        auto *columns=grid->horizontalHeader(),*rows=grid->verticalHeader();
        const QList<QPoint> headers{columns->viewport()->mapTo(area,QPoint(columns->sectionViewportPosition(3)+5,columns->height()/2)),
                                    rows->viewport()->mapTo(area,QPoint(rows->width()/2,rows->sectionViewportPosition(4)+5))};
        for(const auto &target:headers) {
            const QPoint end=dragBy(overlay,QPoint(50,50),target-overlay->geometry().topLeft(),false);
            QCOMPARE(overlay->geometry().topLeft(),target);
            // The dragged chart never paints over a header.
            QVERIFY(!overlay->mask().contains(overlay->mapFrom(area,target)));QVERIFY(!overlay->mask().isEmpty());
            QJsonObject anchor,offset;QVERIFY(!layer->placement(overlay->geometry(),&anchor,&offset));
            QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
            QCOMPARE(overlay->geometry(),before);
        }
        // Outside the grid altogether.
        const QPoint end=dragBy(overlay,QPoint(50,50),QPoint(-3000,-3000),false);
        QJsonObject anchor,offset;QVERIFY(!layer->placement(overlay->geometry(),&anchor,&offset));
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QCOMPARE(overlay->geometry(),before);QCOMPARE(overlay->mask(),QRegion(overlay->rect()));
        QTest::qWait(200);QCOMPARE(admitted.count(),0);QCOMPARE(moved.count(),0);
        QCOMPARE(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",3},{"col",3}}));QCOMPARE(layer->selectedChart(),id);
    }
    void clickWithoutMovingSendsNothing() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Still",2,2,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy moved(layer,&ChartLayer::moveRequested);QSignalSpy resized(layer,&ChartLayer::resizeRequested);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(50,50));QCOMPARE(layer->selectedChart(),id);
        // A press and a release a pixel apart, on the chart and on a handle.
        for(const QPoint &at:{QPoint(50,50),QPoint(overlay->width()-4,overlay->height()-4)}){dragBy(overlay,at,QPoint(1,1));QCOMPARE(overlay->geometry(),before);}
        QTest::qWait(200);QCOMPARE(admitted.count(),0);QCOMPARE(moved.count(),0);QCOMPARE(resized.count(),0);QCOMPARE(layer->selectedChart(),id);
    }
    void handleDragResizes_data() {
        QTest::addColumn<int>("handle");
        QTest::addColumn<int>("cursor");
        const QList<Qt::CursorShape> shapes{Qt::SizeFDiagCursor,Qt::SizeVerCursor,Qt::SizeBDiagCursor,Qt::SizeHorCursor,Qt::SizeFDiagCursor,Qt::SizeVerCursor,Qt::SizeBDiagCursor,Qt::SizeHorCursor};
        const QStringList names{"top-left","top","top-right","right","bottom-right","bottom","bottom-left","left"};
        for(int handle=0;handle<8;++handle)QTest::newRow(qPrintable(QString("%1 %2").arg(handle).arg(names[handle]))) << handle << int(shapes[handle]);
    }
    void handleDragResizes() {
        QFETCH(int,handle);
        QFETCH(int,cursor);
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Sized",3,3,40,30,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();auto *area=overlay->parentWidget();
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(60,60));QCOMPARE(layer->selectedChart(),id);
        // The middle of each handle, clockwise from the top-left.
        const int w=overlay->width(),h=overlay->height(),xs[]{4,(w-8)/2+4,w-4},ys[]{4,(h-8)/2+4,h-4};
        const QPoint points[]{{xs[0],ys[0]},{xs[1],ys[0]},{xs[2],ys[0]},{xs[2],ys[1]},{xs[2],ys[2]},{xs[1],ys[2]},{xs[0],ys[2]},{xs[0],ys[1]}};
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy moved(layer,&ChartLayer::moveRequested);QSignalSpy resized(layer,&ChartLayer::resizeRequested);
        const QPoint delta(-24,-16);const QRect expected=ChartSpec::resized(before,handle,delta);QVERIFY(expected!=before);
        const QPoint end=dragBy(overlay,points[handle],delta,false);
        QCOMPARE(overlay->geometry(),expected);QCOMPARE(int(overlay->cursor().shape()),cursor);QCOMPARE(admitted.count(),0);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QCOMPARE(resized.count(),1);QCOMPARE(moved.count(),0);QCOMPARE(admitted.count(),1);
        // The right, bottom-right and bottom handles keep the stored anchor and
        // offset; the others anchor the new corner where it lies.
        QJsonObject cell{{"row",3},{"col",3}},inside{{"x",40},{"y",30}};
        if(handle!=3 && handle!=4 && handle!=5)QVERIFY(cellUnder(grid,area,expected.topLeft(),&cell,&inside));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["size"].toObject(),(QJsonObject{{"width",expected.width()},{"height",expected.height()}}),10000);
        QCOMPARE(layer->chart(id)["anchor"].toObject(),cell);QCOMPARE(layer->chart(id)["offset"].toObject(),inside);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->geometry(),expected,5000);
    }
    void resizeStopsAtTheMinimum() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Small",3,3,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(60,60));
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        const QPoint end=dragBy(overlay,QPoint(overlay->width()-4,overlay->height()-4),QPoint(-1000,-1000),false);
        QCOMPARE(overlay->geometry(),QRect(before.topLeft(),QSize(64,48)));
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["size"].toObject(),(QJsonObject{{"width",64},{"height",48}}),10000);
        QCOMPARE(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",3},{"col",3}}));QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",0},{"y",0}}));
        QCOMPARE(admitted.count(),1);
        // At the minimum, the top-left handle cannot shrink it further: the
        // opposite corner holds, nothing changes and nothing is sent.
        QVERIFY(idle(window));overlay=layer->overlay(id);QTRY_COMPARE_WITH_TIMEOUT(overlay->size(),QSize(64,48),5000);
        const QRect small=overlay->geometry();
        dragBy(overlay,QPoint(4,4),QPoint(500,500));QCOMPARE(overlay->geometry(),small);
        QTest::qWait(200);QCOMPARE(admitted.count(),1);
    }
    void arrowKeysMoveAndShiftArrowsResize() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Keys",3,3,16,16,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();auto *area=overlay->parentWidget();
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(60,60));QCOMPARE(window.focusWidget(),overlay);
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        // Each arrow is taken before the window's shortcuts.
        for(const auto modifiers:{Qt::KeyboardModifiers(),Qt::KeyboardModifiers(Qt::AltModifier),Qt::KeyboardModifiers(Qt::ShiftModifier),Qt::ShiftModifier|Qt::AltModifier})
            for(const auto key:{Qt::Key_Left,Qt::Key_Right,Qt::Key_Up,Qt::Key_Down}) {
                QKeyEvent override(QEvent::ShortcutOverride,key,modifiers);override.ignore();
                QCoreApplication::sendEvent(overlay,&override);QVERIFY(override.isAccepted());
            }
        struct Step { Qt::Key key; Qt::KeyboardModifiers modifiers; int x,y,width,height; };
        const QList<Step> steps{{Qt::Key_Right,{},24,16,320,200},{Qt::Key_Down,Qt::AltModifier,24,17,320,200},{Qt::Key_Left,{},16,17,320,200},{Qt::Key_Up,Qt::AltModifier,16,16,320,200},
                                {Qt::Key_Right,Qt::ShiftModifier,16,16,328,200},{Qt::Key_Down,Qt::ShiftModifier|Qt::AltModifier,16,16,328,201},
                                {Qt::Key_Left,Qt::ShiftModifier,16,16,320,201},{Qt::Key_Up,Qt::ShiftModifier,16,16,320,193}};
        int sent=0;
        for(const auto &step:steps) {
            QVERIFY(idle(window));QCOMPARE(layer->overlay(id),overlay);
            QTest::keyClick(overlay,step.key,step.modifiers);QCOMPARE(admitted.count(),++sent);
            // Pressed again while that change is in flight, the key is ignored.
            QTest::keyClick(overlay,step.key,step.modifiers);QCOMPARE(admitted.count(),sent);
            QTRY_VERIFY_WITH_TIMEOUT(layer->chart(id)["offset"].toObject()==(QJsonObject{{"x",step.x},{"y",step.y}})
                && layer->chart(id)["size"].toObject()==(QJsonObject{{"width",step.width},{"height",step.height}}),10000);
            QCOMPARE(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",3},{"col",3}}));QCOMPARE(layer->selectedChart(),id);
        }
        // A move past the cell's edge anchors on the next cell.
        const int width=grid->columnWidth(2);
        QVERIFY(addChart(window,opened,"Edge",8,3,width-4,0,200,150));QTRY_VERIFY_WITH_TIMEOUT(overlayNamed(layer,"Edge") && overlayNamed(layer,"Edge")->isVisible(),5000);
        auto *edge=overlayNamed(layer,"Edge");const auto edgeId=edge->chartId();layer->select(edgeId);edge->setFocus();
        QVERIFY(idle(window));QTest::keyClick(edge,Qt::Key_Right);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(edgeId)["anchor"].toObject(),(QJsonObject{{"row",8},{"col",4}}),10000);
        QCOMPARE(layer->chart(edgeId)["offset"].toObject(),(QJsonObject{{"x",4},{"y",0}}));
        // Past the first column or the size bounds the change is refused in the window.
        QVERIFY(addChart(window,opened,"Corner",12,1,0,0,64,48));QTRY_VERIFY_WITH_TIMEOUT(overlayNamed(layer,"Corner") && overlayNamed(layer,"Corner")->isVisible(),5000);
        auto *corner=overlayNamed(layer,"Corner");layer->select(corner->chartId());corner->setFocus();QVERIFY(idle(window));
        const int before=admitted.count();
        QTest::keyClick(corner,Qt::Key_Left);QCOMPARE(statusText(window),QString("The chart cannot move further"));
        QTest::keyClick(corner,Qt::Key_Left,Qt::ShiftModifier);QCOMPARE(statusText(window),QString("The chart cannot resize further"));
        QTest::keyClick(corner,Qt::Key_Down,Qt::AltModifier);QTRY_COMPARE_WITH_TIMEOUT(layer->chart(corner->chartId())["offset"].toObject()["y"].toInt(),1,10000);
        QVERIFY(idle(window));QTest::keyClick(corner,Qt::Key_Up,Qt::ShiftModifier|Qt::AltModifier);QCOMPARE(statusText(window),QString("The chart cannot resize further"));
        QTest::qWait(200);QCOMPARE(admitted.count(),before+1);
        QCOMPARE(layer->chart(corner->chartId())["size"].toObject(),(QJsonObject{{"width",64},{"height",48}}));
        // A chart whose top-left corner is scrolled out of view moves from where it really is.
        layer->select(id);overlay->setFocus();QVERIFY(idle(window));
        auto *bar=grid->verticalScrollBar();
        while(grid->rowViewportPosition(2)+16>=0 && bar->value()<bar->maximum())bar->setValue(bar->value()+1);
        QVERIFY(overlay->isVisible());QVERIFY(overlay->geometry().top()<grid->viewport()->mapTo(area,QPoint()).y());
        QTest::keyClick(overlay,Qt::Key_Down);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",16},{"y",24}}),10000);
        QCOMPARE(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",3},{"col",3}}));
    }
    void deleteKeyDeletesTheChartNotTheCells() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Doomed",6,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();
        grid->setCurrentIndex(model->index(1,1));QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(1,1)),5000);
        QCOMPARE(model->cell(model->index(1,1))["source"].toString(),QString("200"));
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(40,40));QCOMPARE(window.focusWidget(),overlay);
        QKeyEvent override(QEvent::ShortcutOverride,Qt::Key_Delete,Qt::NoModifier);override.ignore();
        QCoreApplication::sendEvent(overlay,&override);QVERIFY(override.isAccepted());
        auto *clear=window.findChild<QAction *>("clearContentsAction");QVERIFY(clear);QSignalSpy cleared(clear,&QAction::triggered);
        QSignalSpy deleted(layer,&ChartLayer::deleteRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        QTest::keyClick(overlay,Qt::Key_Delete);
        QCOMPARE(deleted.count(),1);QCOMPARE(deleted[0][0].toString(),id);QCOMPARE(admitted.count(),1);QCOMPARE(cleared.count(),0);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,10000);QCOMPARE(layer->selectedChart(),QString());QCOMPARE(window.focusWidget(),grid);
        // The cell under the grid cursor keeps its value.
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(1,1)),5000);
        QCOMPARE(model->cell(model->index(1,1))["source"].toString(),QString("200"));QCOMPARE(grid->currentIndex(),model->index(1,1));
        QCOMPARE(admitted.count(),1);QCOMPARE(cleared.count(),0);
    }
    void backspaceDeletes() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Gone",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(40,40));
        QKeyEvent override(QEvent::ShortcutOverride,Qt::Key_Backspace,Qt::NoModifier);override.ignore();
        QCoreApplication::sendEvent(overlay,&override);QVERIFY(override.isAccepted());
        QSignalSpy deleted(layer,&ChartLayer::deleteRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        QTest::keyClick(overlay,Qt::Key_Backspace);
        QCOMPARE(deleted.count(),1);QCOMPARE(deleted[0][0].toString(),id);QCOMPARE(admitted.count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,10000);QCOMPARE(layer->selectedChart(),QString());
        // One Undo restores it.
        QVERIFY(idle(window));window.findChild<QAction *>("undoAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,10000);QCOMPARE(layer->overlays().first()->chartId(),id);
    }
    void contextMenuOffersTheFourCommands() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Menu",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();QVERIFY(idle(window));
        QSignalSpy requested(layer,&ChartLayer::contextMenuRequested);
        // A right-click selects the chart and opens the menu there.
        QTest::mouseClick(overlay,Qt::RightButton,Qt::NoModifier,QPoint(30,30));
        QCOMPARE(layer->selectedChart(),id);QCOMPARE(requested.count(),1);QCOMPARE(requested[0][0].toString(),id);
        QCOMPARE(requested[0][1].toPoint(),overlay->mapToGlobal(QPoint(30,30)));
        auto *menu=window.findChild<QMenu *>("chartContextMenu");QVERIFY(menu);QTRY_VERIFY_WITH_TIMEOUT(menu->isVisible(),5000);
        const auto actions=menu->actions();QCOMPARE(actions.size(),5);
        QCOMPARE(actions[0]->objectName(),QString("editChartAction"));QCOMPARE(actions[0]->text(),QString("Edit Chart"));
        QCOMPARE(actions[1]->objectName(),QString("chartToFrontAction"));QCOMPARE(actions[1]->text(),QString("Bring to Front"));
        QCOMPARE(actions[2]->objectName(),QString("chartToBackAction"));QCOMPARE(actions[2]->text(),QString("Send to Back"));
        QVERIFY(actions[3]->isSeparator());
        QCOMPARE(actions[4]->objectName(),QString("deleteChartAction"));QCOMPARE(actions[4]->text(),QString("Delete"));
        for(int i:{0,1,2,4})QVERIFY2(actions[i]->isEnabled(),qPrintable(actions[i]->objectName()));
        menu->hide();
        QSignalSpy edit(layer,&ChartLayer::editRequested);actions[0]->trigger();QCOMPARE(edit.count(),1);QCOMPARE(edit[0][0].toString(),id);
        // Shift+F10 and the menu key open it on the selected chart.
        overlay->setFocus();
        // Both are taken before any window shortcut, only on a selected chart.
        for(const auto &[key,modifiers]:{std::pair{Qt::Key_F10,Qt::ShiftModifier},std::pair{Qt::Key_Menu,Qt::NoModifier}}) {
            QKeyEvent override(QEvent::ShortcutOverride,key,modifiers);override.ignore();
            QCoreApplication::sendEvent(overlay,&override);QVERIFY(override.isAccepted());
            layer->select({});override.ignore();QCoreApplication::sendEvent(overlay,&override);QVERIFY(!override.isAccepted());layer->select(id);
        }
        QTest::keyClick(overlay,Qt::Key_F10,Qt::ShiftModifier);QCOMPARE(requested.count(),2);QCOMPARE(requested[1][0].toString(),id);
        QTRY_VERIFY_WITH_TIMEOUT(menu->isVisible(),5000);menu->hide();
        QTest::keyClick(overlay,Qt::Key_Menu);QCOMPARE(requested.count(),3);QTRY_VERIFY_WITH_TIMEOUT(menu->isVisible(),5000);menu->hide();
        QVERIFY(overlay->rect().contains(overlay->mapFromGlobal(requested[2][1].toPoint())));
        // Not on a chart that is not selected.
        layer->select({});QTest::keyClick(overlay,Qt::Key_F10,Qt::ShiftModifier);QTest::keyClick(overlay,Qt::Key_Menu);QCOMPARE(requested.count(),3);
        // Delete removes the chart.
        layer->select(id);QSignalSpy deleted(layer,&ChartLayer::deleteRequested);actions[4]->trigger();
        QCOMPARE(deleted.count(),1);QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,10000);
    }
    void bringToFrontAndSendToBack() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Lower",2,2,0,0,320,200));QVERIFY(addChart(window,opened,"Upper",4,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        const auto lower=overlayNamed(layer,"Lower")->chartId(),upper=overlayNamed(layer,"Upper")->chartId();
        auto z=[&](const QString &id){return layer->chart(id)["z"].toInt();};
        auto above=[&](const QString &a,const QString &b){auto *area=layer->overlay(a)->parentWidget();return area->children().indexOf(layer->overlay(a))>area->children().indexOf(layer->overlay(b));};
        QVERIFY(z(lower)<z(upper));QVERIFY(above(upper,lower));
        QSignalSpy ordered(layer,&ChartLayer::orderRequested);
        layer->select(lower);QVERIFY(idle(window));window.findChild<QAction *>("chartToFrontAction")->trigger();
        QCOMPARE(ordered.count(),1);QCOMPARE(ordered[0][0].toString(),lower);QCOMPARE(ordered[0][1].toString(),QString("front"));
        QTRY_VERIFY_WITH_TIMEOUT(z(lower)>z(upper),10000);QVERIFY(above(lower,upper));QCOMPARE(layer->selectedChart(),lower);
        QVERIFY(idle(window));window.findChild<QAction *>("chartToBackAction")->trigger();
        QCOMPARE(ordered.count(),2);QCOMPARE(ordered[1][1].toString(),QString("back"));
        QTRY_VERIFY_WITH_TIMEOUT(z(lower)<z(upper),10000);QVERIFY(above(upper,lower));QCOMPARE(layer->selectedChart(),lower);
    }
    void brokenChartCanBeMovedResizedAndDeleted() {
        CHART_WINDOW;window.submit({{"action","sheet_add"},{"name","Data"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(addChart(window,opened,"Broken chart",2,3,0,0,320,200,"Data"));QVERIFY(addChart(window,opened,"Other",16,3,0,0,200,150));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        auto *tabs=window.findChild<QTabBar *>("sheets");tabs->setCurrentIndex(1);QCOMPARE(tabs->tabText(1),QString("Data"));
        const int before=opened.count();window.submit({{"action","sheet_delete"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);
        QTRY_VERIFY_WITH_TIMEOUT(overlayNamed(layer,"Broken chart") && overlayNamed(layer,"Broken chart")->placeholder().contains("source"),5000);
        auto *broken=overlayNamed(layer,"Broken chart");QTRY_VERIFY_WITH_TIMEOUT(broken->isVisible(),5000);
        const auto id=broken->chartId(),other=overlayNamed(layer,"Other")->chartId();auto *area=broken->parentWidget();
        QVERIFY(ChartSpec::hasBrokenSource(layer->chart(id)));QVERIFY(idle(window));
        // Moved with the mouse.
        const QRect start=broken->geometry();const QPoint delta(40,24);
        dragBy(broken,QPoint(60,60),delta);
        QJsonObject cell,inside;QVERIFY(cellUnder(grid,area,start.topLeft()+delta,&cell,&inside));
        QTRY_VERIFY_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject()==cell && layer->chart(id)["offset"].toObject()==inside,10000);
        // Resized from the keyboard.
        QVERIFY(idle(window));broken=layer->overlay(id);QCOMPARE(window.focusWidget(),broken);
        QTest::keyClick(broken,Qt::Key_Down,Qt::ShiftModifier);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["size"].toObject(),(QJsonObject{{"width",320},{"height",208}}),10000);
        // Reordered.
        QVERIFY(z(layer,id)<z(layer,other));QVERIFY(idle(window));window.findChild<QAction *>("chartToFrontAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(z(layer,id)>z(layer,other),10000);
        // Deleted.
        QVERIFY(idle(window));QCOMPARE(layer->selectedChart(),id);QTest::keyClick(layer->overlay(id),Qt::Key_Delete);
        QTRY_VERIFY_WITH_TIMEOUT(layer->overlay(id)==nullptr,10000);QCOMPARE(layer->overlays().size(),1);
        QVERIFY2(!issuesText(window).contains("chart",Qt::CaseInsensitive),qPrintable(issuesText(window)));
    }
    void rightPressDuringADragCancelsIt() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Pressed",2,2,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();auto *menu=window.findChild<QMenu *>("chartContextMenu");
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy requested(layer,&ChartLayer::contextMenuRequested);QSignalSpy moved(layer,&ChartLayer::moveRequested);
        const QPoint end=dragBy(overlay,QPoint(60,60),QPoint(100,50),false);QCOMPARE(overlay->geometry(),before.translated(100,50));
        // The right press cancels the drag and opens no menu.
        QTest::mousePress(overlay,Qt::RightButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QCOMPARE(overlay->geometry(),before);QCOMPARE(requested.count(),0);QVERIFY(!menu->isVisible());QCOMPARE(layer->selectedChart(),id);
        QTest::mouseRelease(overlay,Qt::RightButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QTest::mouseMove(overlay,overlay->mapFromGlobal(end+QPoint(20,20)));QCOMPARE(overlay->geometry(),before);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end+QPoint(20,20)));
        QTest::qWait(200);QCOMPARE(admitted.count(),0);QCOMPARE(moved.count(),0);QCOMPARE(overlay->geometry(),before);
        // The chart is not stuck: a new drag moves it.
        dragBy(overlay,QPoint(50,50),QPoint(60,30));QCOMPARE(moved.count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->geometry(),before.translated(60,30),10000);
    }
    void dragEndsWhenTheButtonOrFocusIsLost() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Lost",2,2,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        // A move without the button (the release went elsewhere) ends the drag.
        QPoint end=dragBy(overlay,QPoint(60,60),QPoint(100,50),false);QCOMPARE(overlay->geometry(),before.translated(100,50));
        const QPoint at=overlay->mapFromGlobal(end+QPoint(10,10));
        QMouseEvent move(QEvent::MouseMove,QPointF(at),QPointF(overlay->mapToGlobal(at)),Qt::NoButton,Qt::NoButton,Qt::NoModifier);
        QCoreApplication::sendEvent(overlay,&move);
        QCOMPARE(overlay->geometry(),before);QCOMPARE(layer->selectedChart(),id);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,at);QCOMPARE(overlay->geometry(),before);
        // Losing the keyboard (another window activated) ends it too.
        end=dragBy(overlay,QPoint(60,60),QPoint(100,50),false);QCOMPARE(overlay->geometry(),before.translated(100,50));
        QFocusEvent out(QEvent::FocusOut,Qt::ActiveWindowFocusReason);QCoreApplication::sendEvent(overlay,&out);
        QCOMPARE(overlay->geometry(),before);
        QTest::mouseMove(overlay,overlay->mapFromGlobal(end+QPoint(20,20)));QCOMPARE(overlay->geometry(),before);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end+QPoint(20,20)));
        QTest::qWait(200);QCOMPARE(admitted.count(),0);QCOMPARE(overlay->geometry(),before);
    }
    void bottomRightResizeKeepsAScrolledAnchor() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Scrolled",3,3,16,16,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();auto *area=overlay->parentWidget();layer->select(id);
        auto *bar=grid->verticalScrollBar();
        while(grid->rowViewportPosition(2)+16>=0 && bar->value()<bar->maximum())bar->setValue(bar->value()+1);
        QVERIFY(overlay->isVisible());QVERIFY(overlay->geometry().top()<grid->viewport()->mapTo(area,QPoint()).y());
        QSignalSpy resized(layer,&ChartLayer::resizeRequested);
        dragBy(overlay,QPoint(overlay->width()-4,overlay->height()-4),QPoint(24,16));QCOMPARE(resized.count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["size"].toObject(),(QJsonObject{{"width",344},{"height",216}}),10000);
        QCOMPARE(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",3},{"col",3}}));QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",16},{"y",16}}));
    }
    void resizeStopsAtTheMaximum() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Large",3,3,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(60,60));
        const QPoint end=dragBy(overlay,QPoint(overlay->width()-4,overlay->height()-4),QPoint(6000,6000),false);
        QCOMPARE(overlay->geometry(),QRect(before.topLeft(),QSize(4096,4096)));
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["size"].toObject(),(QJsonObject{{"width",4096},{"height",4096}}),10000);
        QCOMPARE(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",3},{"col",3}}));QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",0},{"y",0}}));
    }
    void dragIntoAFrozenColumnAnchorsThere() {
        CHART_WINDOW;window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
        auto *left=window.findChild<QTableView *>("frozenFirstColumn");QVERIFY(left);QTRY_VERIFY_WITH_TIMEOUT(left->isVisible(),5000);
        QVERIFY(addChart(window,opened,"Leftward",4,4,0,0,240,160));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();auto *area=overlay->parentWidget();
        // 7 px into column A and 3 px into row 6, in the frozen column's pane.
        const QPoint target=left->viewport()->mapTo(area,QPoint(left->columnViewportPosition(0)+7,left->rowViewportPosition(5)+3));
        const QPoint end=dragBy(overlay,QPoint(40,40),target-overlay->geometry().topLeft(),false);
        QCOMPARE(overlay->geometry().topLeft(),target);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",6},{"col",1}}),10000);
        QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",7},{"y",3}}));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->geometry().topLeft(),target,5000);
    }
    void dragPlacesNextToAHiddenRow() {
        CHART_WINDOW;selectCells(grid,model,4,0,4,0);window.findChild<QAction *>("hideRowsAction")->trigger();QTRY_VERIFY_WITH_TIMEOUT(grid->isRowHidden(4),5000);
        QVERIFY(addChart(window,opened,"Hopping",10,5,0,0,240,160));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();auto *area=overlay->parentWidget();
        // 2 px below the hidden row 5: row 6, never row 5.
        QCOMPARE(grid->rowViewportPosition(4),grid->rowViewportPosition(5));
        QPoint target=grid->viewport()->mapTo(area,QPoint(grid->columnViewportPosition(2)+5,grid->rowViewportPosition(5)+2));
        QPoint end=dragBy(overlay,QPoint(40,40),target-overlay->geometry().topLeft(),false);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",6},{"col",3}}),10000);
        QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",5},{"y",2}}));
        // The last pixel row above it: row 4.
        QVERIFY(idle(window));overlay=layer->overlay(id);QTRY_COMPARE_WITH_TIMEOUT(overlay->geometry().topLeft(),target,5000);
        const int height=grid->rowHeight(3);
        target=grid->viewport()->mapTo(area,QPoint(grid->columnViewportPosition(1)+3,grid->rowViewportPosition(3)+height-1));
        end=dragBy(overlay,QPoint(40,40),target-overlay->geometry().topLeft(),false);
        QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",4},{"col",2}}),10000);
        QCOMPARE(layer->chart(id)["offset"].toObject(),(QJsonObject{{"x",3},{"y",height-1}}));
    }
    void dockOpensOnInsertEditAndDoubleClickNotOnAClick() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));activate(window);
        auto *dock=window.findChild<QDockWidget *>("chartDock");QVERIFY(dock);auto *panel=panelOf(window);QVERIFY(panel);
        QCOMPARE(dock->windowTitle(),QString("Chart"));QCOMPARE(window.dockWidgetArea(dock),Qt::RightDockWidgetArea);QVERIFY(!dock->isVisible());
        auto *scroll=qobject_cast<QScrollArea *>(dock->widget());QVERIFY(scroll);QCOMPARE(scroll->widget(),panel);
        QCOMPARE(scroll->horizontalScrollBarPolicy(),Qt::ScrollBarAlwaysOff);
        // View → Chart sits next to View → Issues.
        QMenu *view=nullptr;for(auto *action:window.menuBar()->actions())if(action->text()=="&View")view=action->menu();QVERIFY(view);
        const auto actions=view->actions();auto *issues=window.findChild<QDockWidget *>("issuesDock")->toggleViewAction();
        const int at=int(actions.indexOf(issues));QVERIFY(at>=0);QCOMPARE(actions.value(at+1),dock->toggleViewAction());QCOMPARE(dock->toggleViewAction()->text(),QString("Chart"));
        // Insert opens the panel on the new chart; focus stays on the chart.
        selectCells(grid,model,0,0,3,1);window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(!layer->selectedChart().isEmpty(),10000);const auto id=layer->selectedChart();
        QTRY_VERIFY_WITH_TIMEOUT(dock->isVisible(),5000);QCOMPARE(panel->chartId(),id);
        QCOMPARE(window.findChild<QLineEdit *>("chartName")->text(),QString("Chart 1"));
        QTRY_COMPARE_WITH_TIMEOUT(window.focusWidget(),static_cast<QWidget *>(layer->overlay(id)),5000);
        // A click only selects.
        dock->hide();QVERIFY(idle(window));layer->select({});auto *overlay=layer->overlay(id);
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(40,40));QCOMPARE(layer->selectedChart(),id);
        QTest::qWait(100);QVERIFY(!dock->isVisible());
        // A double-click opens it.
        QTest::mouseDClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(40,40));QVERIFY(dock->isVisible());QCOMPARE(panel->chartId(),id);
        // Enter on the chart opens it and puts focus on Name.
        dock->hide();overlay->setFocus();QCOMPARE(window.focusWidget(),overlay);
        QTest::keyClick(overlay,Qt::Key_Return);QVERIFY(dock->isVisible());
        QCOMPARE(window.focusWidget(),static_cast<QWidget *>(window.findChild<QLineEdit *>("chartName")));
        // Edit Chart the same.
        dock->hide();overlay->setFocus();window.findChild<QAction *>("editChartAction")->trigger();QVERIFY(dock->isVisible());
        QCOMPARE(window.focusWidget(),static_cast<QWidget *>(window.findChild<QLineEdit *>("chartName")));
        // View → Chart hides and shows it.
        dock->toggleViewAction()->trigger();QVERIFY(!dock->isVisible());dock->toggleViewAction()->trigger();QVERIFY(dock->isVisible());
    }
    void panelSaysNoChartSelected() {
        CHART_WINDOW;window.submit({{"action","sheet_add"},{"name","Other"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(addChart(window,opened,"Shown",2,4));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);const auto id=overlay->chartId();
        auto *dock=window.findChild<QDockWidget *>("chartDock");dock->toggleViewAction()->trigger();QVERIFY(dock->isVisible());
        auto *panel=panelOf(window);auto *name=window.findChild<QLineEdit *>("chartName");
        auto empty=[&]{const auto *label=noChartLabel(panel);return label && label->isVisible() && !name->isVisible() && panel->chartId().isEmpty();};
        QVERIFY(empty());
        layer->select(id);QCOMPARE(panel->chartId(),id);QVERIFY(name->isVisible());QCOMPARE(name->text(),QString("Shown"));QVERIFY(!noChartLabel(panel)->isVisible());
        // A click on the grid clears it.
        QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,QPoint(grid->columnViewportPosition(1)+5,grid->rowViewportPosition(1)+5));QVERIFY(empty());
        // Escape on the chart clears it.
        QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(20,20));QCOMPARE(panel->chartId(),id);
        QTest::keyClick(overlay,Qt::Key_Escape);QVERIFY(empty());
        // A sheet switch clears it.
        layer->select(id);QCOMPARE(panel->chartId(),id);
        window.findChild<QTabBar *>("sheets")->setCurrentIndex(1);QVERIFY(empty());QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,5000);QVERIFY(empty());
        QVERIFY(dock->isVisible());
    }
    void renameAppliesOnEnterAndOnFocusOut() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Before",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();
        auto *panel=panelOf(window);QVERIFY(openPanel(window,layer,id));auto *name=window.findChild<QLineEdit *>("chartName");
        QCOMPARE(name->text(),QString("Before"));QCOMPARE(name->maxLength(),64);
        QSignalSpy requested(panel,&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        typeInto(name,"Entered",true);
        QCOMPARE(requested.count(),1);QCOMPARE(requested[0][0].toString(),id);
        QCOMPARE(requested[0][1].toJsonObject(),(QJsonObject{{"name","Entered"}}));QCOMPARE(requested[0][2].toString(),QString("chartName"));
        QCOMPARE(admitted.count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["name"].toString(),QString("Entered"),10000);QTRY_COMPARE_WITH_TIMEOUT(name->text(),QString("Entered"),5000);
        // Leaving the field applies it too.
        QVERIFY(idle(window));typeInto(name,"Left behind",false);QCOMPARE(requested.count(),1);
        window.findChild<QLineEdit *>("chartTitle")->setFocus();
        QCOMPARE(requested.count(),2);QCOMPARE(requested[1][1].toJsonObject(),(QJsonObject{{"name","Left behind"}}));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["name"].toString(),QString("Left behind"),10000);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->accessibleName(),QString("Chart: Left behind"),5000);
        QCOMPARE(admitted.count(),2);
    }
    void eachTypeButtonChangesTheType_data() {
        QTest::addColumn<QString>("type");
        for(const char *type:{"column","bar","line","area","pie","scatter"})QTest::newRow(type) << QString(type);
    }
    void eachTypeButtonChangesTheType() {
        QFETCH(QString,type);
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        auto spec=chartSpec("Sheet1");spec["type"]=type=="column"?"line":"column";
        QVERIFY(addChart(window,opened,"Typed",2,4,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *button=window.findChild<QToolButton *>("chartType_"+type);QVERIFY(button);QVERIFY(button->isCheckable());QVERIFY(!button->isChecked());
        auto *from=window.findChild<QToolButton *>("chartType_"+spec["type"].toString());QVERIFY(from->isChecked());
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        QTest::mouseClick(button,Qt::LeftButton);QCOMPARE(admitted.count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["type"].toString(),type,10000);
        QTRY_VERIFY_WITH_TIMEOUT(button->isChecked() && !from->isChecked(),5000);
        const auto stored=layer->chart(id)["spec"].toObject();
        QCOMPARE(stored["series"].toArray().size(),1);
        auto *grouping=window.findChild<QComboBox *>("chartGrouping");
        QVERIFY(idle(window));QCOMPARE(grouping->isEnabled(),ChartSpec::takesGrouping(type));
        if(ChartSpec::takesGrouping(type)){QCOMPARE(stored["grouping"].toString(),QString("clustered"));QCOMPARE(grouping->currentText(),QString("Clustered"));}
        else {QVERIFY(stored["grouping"].isNull());QCOMPARE(grouping->currentIndex(),-1);}
        // A click on the pressed button sends nothing.
        QTest::mouseClick(button,Qt::LeftButton);QTest::qWait(100);QCOMPARE(admitted.count(),1);
        QCOMPARE(window.findChild<QAction *>("undoAction")->text(),QString("Undo Edit chart"));
    }
    void typeChangeCarriesItsCompanions() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        auto spec=chartSpec("Sheet1");spec["grouping"]="stacked";spec["title"]=QJsonObject{{"text","Kept"}};spec["legend"]=QJsonObject{{"position","bottom"}};
        QVERIFY(addChart(window,opened,"Companions",2,4,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QCOMPARE(window.findChild<QComboBox *>("chartGrouping")->currentText(),QString("Stacked"));
        // To scatter: the categories become every series' X; no grouping.
        QTest::mouseClick(window.findChild<QToolButton *>("chartType_scatter"),Qt::LeftButton);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["type"].toString(),QString("scatter"),10000);
        auto stored=layer->chart(id)["spec"].toObject();
        QVERIFY(stored["categories"].isNull());QVERIFY(stored["grouping"].isNull());
        QCOMPARE(stored["series"].toArray()[0].toObject()["x"].toObject()["source"].toObject()["ref"].toString(),QString("A2:A4"));
        QCOMPARE(stored["title"].toObject()["text"].toString(),QString("Kept"));QCOMPARE(stored["legend"].toObject()["position"].toString(),QString("bottom"));
        // And back: the first series' X becomes the categories; grouping Clustered.
        QVERIFY(idle(window));QTest::mouseClick(window.findChild<QToolButton *>("chartType_column"),Qt::LeftButton);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["type"].toString(),QString("column"),10000);
        stored=layer->chart(id)["spec"].toObject();
        QCOMPARE(stored["categories"].toObject()["source"].toObject()["ref"].toString(),QString("A2:A4"));
        QVERIFY(stored["series"].toArray()[0].toObject()["x"].isNull());QCOMPARE(stored["grouping"].toString(),QString("clustered"));
        QCOMPARE(stored["series"].toArray()[0].toObject()["values"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B4"));
        QCOMPARE(stored["title"].toObject()["text"].toString(),QString("Kept"));QCOMPARE(stored["legend"].toObject()["position"].toString(),QString("bottom"));
        // A chart without categories cannot become a scatter: refused in the panel, nothing sent.
        auto bare=chartSpec("Sheet1");bare.remove("categories");bare["type"]="line";
        QVERIFY(idle(window));QVERIFY(addChart(window,opened,"Bare",12,4,0,0,320,200,"Sheet1",bare));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        const auto bareId=overlayNamed(layer,"Bare")->chartId();QVERIFY(openPanel(window,layer,bareId));
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        QTest::mouseClick(window.findChild<QToolButton *>("chartType_scatter"),Qt::LeftButton);
        auto *message=window.findChild<QLabel *>("chartTypeMessage");QVERIFY(message);QVERIFY(message->isVisible());
        QCOMPARE(message->text(),QString("Give an X source before changing this chart to a scatter chart."));
        QVERIFY(window.findChild<QToolButton *>("chartType_line")->isChecked());QVERIFY(!window.findChild<QToolButton *>("chartType_scatter")->isChecked());
        QTest::qWait(100);QCOMPARE(admitted.count(),0);QCOMPARE(layer->chart(bareId)["spec"].toObject()["type"].toString(),QString("line"));
    }
    void twoSeriesToPieIsRefusedAndTheTypeGoesBack() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        auto spec=chartSpec("Sheet1");auto list=spec["series"].toArray();list.append(list.first());spec["series"]=list;
        QVERIFY(addChart(window,opened,"Pair",2,4,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        const auto before=layer->chart(id);
        QSignalSpy receipts(&window,&MainWindow::operationReceipt);
        auto *pie=window.findChild<QToolButton *>("chartType_pie"),*column=window.findChild<QToolButton *>("chartType_column");
        QTest::mouseClick(pie,Qt::LeftButton);
        QTRY_COMPARE_WITH_TIMEOUT(receipts.count(),1,10000);const auto receipt=receipts[0][0].toJsonObject();
        QVERIFY(receipt["outcome"]=="rejected" || receipt["outcome"]=="rolled_back");const auto serviceMessage=receipt["message"].toString();
        QVERIFY2(serviceMessage.contains("a pie chart has exactly one series"),qPrintable(serviceMessage));
        auto *message=window.findChild<QLabel *>("chartTypeMessage");QVERIFY(message);
        QTRY_VERIFY_WITH_TIMEOUT(message->isVisible(),5000);QCOMPARE(message->text(),serviceMessage);QVERIFY(message->wordWrap());
        QCOMPARE(column->accessibleDescription(),serviceMessage);
        QVERIFY(column->isChecked());QVERIFY(!pie->isChecked());
        QVERIFY(issuesText(window).contains(serviceMessage));
        // The chart is unchanged: two series, still a column chart.
        QTest::qWait(200);QCOMPARE(layer->chart(id),before);QCOMPARE(layer->chart(id)["spec"].toObject()["series"].toArray().size(),2);
        // The next edit of the type clears the message.
        QVERIFY(idle(window));QTest::mouseClick(window.findChild<QToolButton *>("chartType_line"),Qt::LeftButton);QVERIFY(!message->isVisible());
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["type"].toString(),QString("line"),10000);
        QCOMPARE(layer->chart(id)["spec"].toObject()["series"].toArray().size(),2);
    }
    void titleAxisTitlesMinMaxAndLegendApply() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Dressed",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto specOfChart=[&]{return layer->chart(id)["spec"].toObject();};
        auto valueAxis=[&]{return specOfChart()["axes"].toObject()["value"].toObject();};
        auto *title=window.findChild<QLineEdit *>("chartTitle");QCOMPARE(title->maxLength(),255);QCOMPARE(title->accessibleName(),QString("Title"));
        typeInto(title,"Units by month",true);
        QTRY_COMPARE_WITH_TIMEOUT(specOfChart()["title"].toObject()["text"].toString(),QString("Units by month"),10000);
        QVERIFY(idle(window));typeInto(window.findChild<QLineEdit *>("categoryAxisTitle"),"Month",true);
        QTRY_COMPARE_WITH_TIMEOUT(specOfChart()["axes"].toObject()["category"].toObject()["title"].toString(),QString("Month"),10000);
        QVERIFY(idle(window));typeInto(window.findChild<QLineEdit *>("valueAxisTitle"),"Units",true);
        QTRY_COMPARE_WITH_TIMEOUT(valueAxis()["title"].toString(),QString("Units"),10000);
        auto *minimum=window.findChild<QLineEdit *>("valueAxisMin"),*maximum=window.findChild<QLineEdit *>("valueAxisMax");
        QCOMPARE(minimum->placeholderText(),QString("Automatic"));QCOMPARE(maximum->placeholderText(),QString("Automatic"));
        QVERIFY(idle(window));typeInto(minimum,"10",true);
        QTRY_VERIFY_WITH_TIMEOUT(valueAxis()["min"].isDouble(),10000);QCOMPARE(valueAxis()["min"].toDouble(),10.0);
        QVERIFY(idle(window));typeInto(maximum,"2.5e3",true);
        QTRY_VERIFY_WITH_TIMEOUT(valueAxis()["max"].isDouble(),10000);QCOMPARE(valueAxis()["max"].toDouble(),2500.0);
        QTRY_COMPARE_WITH_TIMEOUT(maximum->text(),QString("2500"),5000);
        auto *legend=window.findChild<QComboBox *>("legendPosition");QCOMPARE(legend->currentText(),QString("Right"));QCOMPARE(legend->accessibleName(),QString("Legend"));
        QVERIFY(idle(window));choose(legend,"Bottom");
        QTRY_COMPARE_WITH_TIMEOUT(specOfChart()["legend"].toObject()["position"].toString(),QString("bottom"),10000);
        QVERIFY(idle(window));choose(legend,"None");
        QTRY_COMPARE_WITH_TIMEOUT(specOfChart()["legend"].toObject()["position"].toString(),QString("none"),10000);
        QVERIFY(idle(window));choose(window.findChild<QComboBox *>("chartGrouping"),"Stacked");
        QTRY_COMPARE_WITH_TIMEOUT(specOfChart()["grouping"].toString(),QString("stacked"),10000);
        // An emptied title means no title.
        QVERIFY(idle(window));typeInto(title,"",true);
        QTRY_VERIFY_WITH_TIMEOUT(specOfChart()["title"].toObject()["text"].isNull(),10000);
        // Text that is not a number is refused beside the field, without a request.
        QVERIFY(idle(window));QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        typeInto(minimum,"ten",true);
        auto *message=window.findChild<QLabel *>("valueAxisMinMessage");QVERIFY(message && message->isVisible());
        QCOMPARE(message->text(),QString("Enter a number or leave empty for automatic"));QCOMPARE(minimum->text(),QString("10"));
        QTest::qWait(100);QCOMPARE(admitted.count(),0);QCOMPARE(valueAxis()["min"].toDouble(),10.0);
        // Everything else held.
        const auto stored=specOfChart();
        QCOMPARE(stored["axes"].toObject()["category"].toObject()["title"].toString(),QString("Month"));
        QCOMPARE(valueAxis()["title"].toString(),QString("Units"));QCOMPARE(valueAxis()["max"].toDouble(),2500.0);
        QCOMPARE(stored["series"].toArray().size(),1);QCOMPARE(stored["type"].toString(),QString("column"));
    }
    void emptyMinimumMeansAutomatic() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        auto spec=chartSpec("Sheet1");spec["axes"]=QJsonObject{{"category",QJsonObject{{"title",QJsonValue::Null}}},{"value",QJsonObject{{"title",QJsonValue::Null},{"min",5},{"max",50}}}};
        QVERIFY(addChart(window,opened,"Bounded",2,4,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));activate(window);
        auto *minimum=window.findChild<QLineEdit *>("valueAxisMin"),*maximum=window.findChild<QLineEdit *>("valueAxisMax");
        QCOMPARE(minimum->text(),QString("5"));QCOMPARE(maximum->text(),QString("50"));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);
        typeInto(minimum,"",false);maximum->setFocus();
        QCOMPARE(requested.count(),1);
        const auto sent=requested[0][1].toJsonObject()["spec"].toObject()["axes"].toObject()["value"].toObject();
        QVERIFY(sent["min"].isNull());QCOMPARE(sent["max"].toDouble(),50.0);
        auto valueAxis=[&]{return layer->chart(id)["spec"].toObject()["axes"].toObject()["value"].toObject();};
        QTRY_VERIFY_WITH_TIMEOUT(valueAxis()["min"].isNull(),10000);QCOMPARE(valueAxis()["max"].toDouble(),50.0);
        QTRY_COMPARE_WITH_TIMEOUT(minimum->text(),QString(),5000);QCOMPARE(minimum->placeholderText(),QString("Automatic"));
    }
    void minimumAboveMaximumIsRefused() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        auto spec=chartSpec("Sheet1");spec["axes"]=QJsonObject{{"category",QJsonObject{{"title",QJsonValue::Null}}},{"value",QJsonObject{{"title",QJsonValue::Null},{"min",QJsonValue::Null},{"max",10}}}};
        QVERIFY(addChart(window,opened,"Inverted",2,4,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        const auto before=layer->chart(id);
        QSignalSpy receipts(&window,&MainWindow::operationReceipt);
        auto *minimum=window.findChild<QLineEdit *>("valueAxisMin");typeInto(minimum,"20",true);
        QTRY_COMPARE_WITH_TIMEOUT(receipts.count(),1,10000);const auto outcome=receipts[0][0].toJsonObject()["outcome"].toString();QVERIFY(outcome=="rejected" || outcome=="rolled_back");
        const auto serviceMessage=receipts[0][0].toJsonObject()["message"].toString();QVERIFY2(serviceMessage.contains("min must not exceed max"),qPrintable(serviceMessage));
        auto *message=window.findChild<QLabel *>("valueAxisMinMessage");QVERIFY(message);
        QTRY_VERIFY_WITH_TIMEOUT(message->isVisible(),5000);QCOMPARE(message->text(),serviceMessage);QCOMPARE(minimum->accessibleDescription(),serviceMessage);
        QCOMPARE(minimum->text(),QString());
        QTest::qWait(200);QCOMPARE(layer->chart(id),before);
        // Editing the field again clears the message.
        QVERIFY(idle(window));minimum->setFocus();QTest::keyClicks(minimum,"1");QVERIFY(!message->isVisible());QVERIFY(minimum->accessibleDescription().isEmpty());
    }
    void frontAndBackButtonsReorder() {
        CHART_WINDOW;QVERIFY(addChart(window,opened,"Lower",2,2,0,0,320,200));QVERIFY(addChart(window,opened,"Upper",4,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        const auto lower=overlayNamed(layer,"Lower")->chartId(),upper=overlayNamed(layer,"Upper")->chartId();
        QVERIFY(z(layer,lower)<z(layer,upper));QVERIFY(openPanel(window,layer,lower));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);
        auto *front=window.findChild<QPushButton *>("chartToFront"),*back=window.findChild<QPushButton *>("chartToBack");QVERIFY(front && back);
        QCOMPARE(front->text(),QString("Bring to Front"));QCOMPARE(back->text(),QString("Send to Back"));
        QTest::mouseClick(front,Qt::LeftButton);QCOMPARE(requested.count(),1);QCOMPARE(requested[0][1].toJsonObject(),(QJsonObject{{"order","front"}}));
        QTRY_VERIFY_WITH_TIMEOUT(z(layer,lower)>z(layer,upper),10000);QCOMPARE(layer->selectedChart(),lower);
        QVERIFY(idle(window));QTest::mouseClick(back,Qt::LeftButton);QCOMPARE(requested.count(),2);QCOMPARE(requested[1][1].toJsonObject(),(QJsonObject{{"order","back"}}));
        QTRY_VERIFY_WITH_TIMEOUT(z(layer,lower)<z(layer,upper),10000);QCOMPARE(panelOf(window)->chartId(),lower);
    }
    void everyPanelChangeIsOneUndo() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Original",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *undo=window.findChild<QAction *>("undoAction");
        auto *name=window.findChild<QLineEdit *>("chartName"),*title=window.findChild<QLineEdit *>("chartTitle");auto *legend=window.findChild<QComboBox *>("legendPosition");
        auto *column=window.findChild<QToolButton *>("chartType_column"),*line=window.findChild<QToolButton *>("chartType_line");
        auto spec=[&]{return layer->chart(id)["spec"].toObject();};
        const int before=opened.count();
        typeInto(name,"Renamed",true);QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["name"].toString(),QString("Renamed"),10000);
        QVERIFY(idle(window));QCOMPARE(undo->text(),QString("Undo Edit chart"));
        QTest::mouseClick(line,Qt::LeftButton);QTRY_COMPARE_WITH_TIMEOUT(spec()["type"].toString(),QString("line"),10000);
        QVERIFY(idle(window));QCOMPARE(undo->text(),QString("Undo Edit chart"));
        typeInto(title,"Titled",true);QTRY_COMPARE_WITH_TIMEOUT(spec()["title"].toObject()["text"].toString(),QString("Titled"),10000);
        QVERIFY(idle(window));QCOMPARE(undo->text(),QString("Undo Edit chart"));
        choose(legend,"None");QTRY_COMPARE_WITH_TIMEOUT(spec()["legend"].toObject()["position"].toString(),QString("none"),10000);
        QVERIFY(idle(window));QCOMPARE(undo->text(),QString("Undo Edit chart"));
        QCOMPARE(opened.count(),before+4);
        // Each Undo reverses exactly one change, newest first, and the panel follows.
        undo->trigger();QTRY_COMPARE_WITH_TIMEOUT(spec()["legend"].toObject()["position"].toString(),QString("right"),10000);
        QCOMPARE(spec()["title"].toObject()["text"].toString(),QString("Titled"));QTRY_COMPARE_WITH_TIMEOUT(legend->currentText(),QString("Right"),5000);
        QVERIFY(idle(window));undo->trigger();QTRY_VERIFY_WITH_TIMEOUT(spec()["title"].toObject()["text"].isNull(),10000);
        QCOMPARE(spec()["type"].toString(),QString("line"));QTRY_COMPARE_WITH_TIMEOUT(title->text(),QString(),5000);
        QVERIFY(idle(window));undo->trigger();QTRY_COMPARE_WITH_TIMEOUT(spec()["type"].toString(),QString("column"),10000);
        QCOMPARE(layer->chart(id)["name"].toString(),QString("Renamed"));QTRY_VERIFY_WITH_TIMEOUT(column->isChecked() && !line->isChecked(),5000);
        QVERIFY(idle(window));undo->trigger();QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["name"].toString(),QString("Original"),10000);
        QTRY_COMPARE_WITH_TIMEOUT(name->text(),QString("Original"),5000);QCOMPARE(panelOf(window)->chartId(),id);
    }
    void panelClearsWhenItsChartIsUndoneAway() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));activate(window);
        selectCells(grid,model,0,0,3,1);window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(!layer->selectedChart().isEmpty(),10000);const auto id=layer->selectedChart();QVERIFY(idle(window));
        QVERIFY(openPanel(window,layer,id));auto *panel=panelOf(window);
        auto *title=window.findChild<QLineEdit *>("chartTitle");
        QSignalSpy requested(panel,&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        // Directly: the chart goes from an unlocked panel while a field holds edited text; its focus-out sends nothing.
        title->setFocus();QTest::keyClicks(title,"Gone with it");QVERIFY(title->isEnabled());
        panel->setChart({});QVERIFY(noChartLabel(panel)->isVisible());
        grid->setFocus();QFocusEvent away(QEvent::FocusOut,Qt::OtherFocusReason);QCoreApplication::sendEvent(title,&away);
        QTest::keyClick(title,Qt::Key_Return);
        QTest::qWait(100);QCOMPARE(requested.count(),0);QCOMPARE(admitted.count(),0);
        // Shown again, the chart's field shows its stored value and nothing was held.
        panel->setChart(layer->chart(id));QCOMPARE(title->text(),QString());QTest::qWait(100);QCOMPARE(requested.count(),0);
        // End to end: Undo of the creation while the title has focus and unsent text.
        title->setFocus();QCOMPARE(window.focusWidget(),static_cast<QWidget *>(title));
        QTest::keyClicks(title,"Never sent");
        // Undo the creation from the Edit menu while the title has focus and unsent text.
        QMenu *edit=nullptr;for(auto *action:window.menuBar()->actions())if(action->text()=="&Edit")edit=action->menu();QVERIFY(edit);
        QAction *undo=nullptr;for(auto *action:edit->actions())if(action->objectName()=="undoAction")undo=action;QVERIFY(undo && undo->isEnabled());
        undo->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),0,10000);QCOMPARE(layer->selectedChart(),QString());
        QTRY_VERIFY_WITH_TIMEOUT(panel->chartId().isEmpty() && noChartLabel(panel)->isVisible(),5000);
        // Nothing is left to edit, locked or not.
        QVERIFY(idle(window));panel->setLocked(true);panel->setLocked(false);
        for(const char *field:{"chartName","chartTitle","chartToFront"})QVERIFY2(!window.findChild<QWidget *>(field)->isEnabled(),field);
        // The stale field's focus-out, and Enter on it, send nothing.
        QTest::keyClick(title,Qt::Key_Return);
        QFocusEvent out(QEvent::FocusOut,Qt::OtherFocusReason);QCoreApplication::sendEvent(title,&out);title->clearFocus();
        QVERIFY(idle(window));QTest::qWait(200);
        QCOMPARE(requested.count(),0);QCOMPARE(admitted.count(),1);QCOMPARE(layer->overlays().size(),0);
        QVERIFY(!issuesText(window).contains("chart",Qt::CaseInsensitive));
    }
    void unsupportedVersionShowsTheReasonAndOnlyTheChartGroup() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Future",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();
        // Another program stores a newer specification version (direct SQLite access).
        QProcess direct;direct.start(qEnvironmentVariable("DATAHORIZON_PYTHON","python3"),{"-c",
            "import json,sqlite3,sys\n"
            "db=sqlite3.connect(sys.argv[1])\n"
            "for i,p in db.execute(\"SELECT id,payload FROM dh_objects WHERE namespace='chart'\").fetchall():\n"
            "    d=json.loads(p);d['spec_version']=2;d['spec']['spec_version']=2\n"
            "    db.execute(\"UPDATE dh_objects SET payload=? WHERE namespace='chart' AND id=?\",(json.dumps(d),i))\n"
            "db.commit();db.close()\n",temp.filePath("charts.dhwb")});
        QVERIFY(direct.waitForFinished(10000));QCOMPARE(direct.exitCode(),0);
        window.findChild<QAction *>("refreshStoredDataAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["issue"].toString(),QString("UNSUPPORTED_SPEC_VERSION"),10000);
        QVERIFY(idle(window));QVERIFY(openPanel(window,layer,id));
        auto *reason=window.findChild<QLabel *>("chartPanelReason");QVERIFY(reason);QVERIFY(reason->isVisible());
        QCOMPARE(reason->text(),QString("This chart cannot be read (UNSUPPORTED_SPEC_VERSION)"));
        auto *name=window.findChild<QLineEdit *>("chartName");
        for(const char *enabled:{"chartName","chartToFront","chartToBack"})QVERIFY2(window.findChild<QWidget *>(enabled)->isEnabled(),enabled);
        for(const char *type:{"column","bar","line","area","pie","scatter"}) {
            auto *button=window.findChild<QToolButton *>(QString("chartType_")+type);QVERIFY2(!button->isEnabled() && !button->isChecked(),type);
        }
        for(const char *disabled:{"chartGrouping","chartTitle","categoryAxisTitle","valueAxisTitle","valueAxisMin","valueAxisMax","legendPosition"})QVERIFY2(!window.findChild<QWidget *>(disabled)->isEnabled(),disabled);
        for(const char *empty:{"chartTitle","categoryAxisTitle","valueAxisTitle","valueAxisMin","valueAxisMax"})QVERIFY2(window.findChild<QLineEdit *>(empty)->text().isEmpty(),empty);
        QCOMPARE(window.findChild<QComboBox *>("chartGrouping")->currentIndex(),-1);QCOMPARE(window.findChild<QComboBox *>("legendPosition")->currentIndex(),-1);
        // Locking and unlocking (a busy spell) keeps the specification groups disabled.
        panelOf(window)->setLocked(true);panelOf(window)->setLocked(false);
        QVERIFY(name->isEnabled());QVERIFY(!window.findChild<QWidget *>("chartTitle")->isEnabled());QVERIFY(!window.findChild<QWidget *>("chartType_line")->isEnabled());
        // A rename sends only the name and leaves the specification alone.
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);
        typeInto(name,"Renamed",true);QCOMPARE(requested.count(),1);QCOMPARE(requested[0][1].toJsonObject(),(QJsonObject{{"name","Renamed"}}));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["name"].toString(),QString("Renamed"),10000);
        QCOMPARE(layer->chart(id)["issue"].toString(),QString("UNSUPPORTED_SPEC_VERSION"));
        QVERIFY(idle(window));QCOMPARE(reason->text(),QString("This chart cannot be read (UNSUPPORTED_SPEC_VERSION)"));QVERIFY(!window.findChild<QWidget *>("chartTitle")->isEnabled());
        QVERIFY(!issuesText(window).contains("UNSUPPORTED"));
    }
    void focusReturnsAfterAChange() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Focused",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *title=window.findChild<QLineEdit *>("chartTitle");
        // Tab from Name into Title, type, Enter.
        for(int i=0;i<20 && window.focusWidget()!=title;++i)QTest::keyClick(window.focusWidget(),Qt::Key_Tab);
        QCOMPARE(window.focusWidget(),static_cast<QWidget *>(title));
        QTest::keyClicks(title,"Kept in place");QTest::keyClick(title,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["title"].toObject()["text"].toString(),QString("Kept in place"),10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.focusWidget(),static_cast<QWidget *>(title),5000);QVERIFY(title->isEnabled());
        // A list changed from the keyboard keeps the focus too.
        QVERIFY(idle(window));auto *legend=window.findChild<QComboBox *>("legendPosition");choose(legend,"Bottom");
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["legend"].toObject()["position"].toString(),QString("bottom"),10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.focusWidget(),static_cast<QWidget *>(legend),5000);QVERIFY(legend->isEnabled());
    }
    void sourceFieldsShowTheStoredSources() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addTableAndQuery(window,opened));
        QVERIFY(applied(window,opened,{{"action","sheet_add"},{"name","Other Data"}}));
        auto range=[](const QString &sheet,const QString &ref){return QJsonObject{{"source",QJsonObject{{"kind","range"},{"sheet",sheet},{"ref",ref}}}};};
        const QJsonObject spec{{"spec_version",1},{"type","column"},{"categories",range("Sheet1","A2:A4")},{"series",QJsonArray{
            QJsonObject{{"name",range("Sheet1","B1")},{"values",range("Sheet1","B2:B4")}},
            QJsonObject{{"name",QJsonObject{{"text","Tab"}}},{"values",QJsonObject{{"source",QJsonObject{{"kind","table"},{"table","Orders"},{"column","Quantity"}}}}}},
            QJsonObject{{"name",range("Other Data","A1")},{"values",QJsonObject{{"source",QJsonObject{{"kind","query"},{"query","Totals"},{"column","Total"}}}}}}}}};
        QVERIFY(addChart(window,opened,"Sources",2,6,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        // Every source field starts with its kind list.
        auto *categories=kindOf(window,"categoriesSource");QVERIFY(categories);
        QCOMPARE(itemsOf(categories),(QStringList{"Range","Table column","Query column"}));
        QCOMPARE(categories->currentText(),QString("Range"));QCOMPARE(rangeOf(window,"categoriesSource")->text(),QString("A2:A4"));
        QCOMPARE(itemsOf(kindOf(window,"series1Name")),(QStringList{"Text","Cell"}));
        // Ranges on the chart's own sheet are bare; on another sheet, qualified.
        QCOMPARE(kindOf(window,"series1Name")->currentText(),QString("Cell"));QCOMPARE(rangeOf(window,"series1Name")->text(),QString("B1"));
        QCOMPARE(kindOf(window,"series1Values")->currentText(),QString("Range"));QCOMPARE(rangeOf(window,"series1Values")->text(),QString("B2:B4"));
        QCOMPARE(kindOf(window,"series3Name")->currentText(),QString("Cell"));QCOMPARE(rangeOf(window,"series3Name")->text(),QString("'Other Data'!A1"));
        // A text name shows the text without a Pick button.
        QCOMPARE(kindOf(window,"series2Name")->currentText(),QString("Text"));QCOMPARE(rangeOf(window,"series2Name")->text(),QString("Tab"));
        QVERIFY(!window.findChild<QToolButton *>("series2NamePick")->isVisible());QVERIFY(window.findChild<QToolButton *>("series1NamePick")->isVisible());
        // A table column and a query column show their two lists.
        QCOMPARE(kindOf(window,"series2Values")->currentText(),QString("Table column"));
        auto *table=window.findChild<QComboBox *>("series2ValuesTable"),*tableColumn=window.findChild<QComboBox *>("series2ValuesTableColumn");
        QVERIFY(table && tableColumn && table->isVisible());QVERIFY(!rangeOf(window,"series2Values")->isVisible());
        QCOMPARE(table->currentText(),QString("Orders"));QCOMPARE(tableColumn->currentText(),QString("Quantity"));
        QCOMPARE(kindOf(window,"series3Values")->currentText(),QString("Query column"));
        auto *query=window.findChild<QComboBox *>("series3ValuesQuery"),*queryColumn=window.findChild<QComboBox *>("series3ValuesQueryColumn");
        QVERIFY(query && queryColumn && query->isVisible());QCOMPARE(query->currentText(),QString("Totals"));QCOMPARE(queryColumn->currentText(),QString("Total"));
        // The workbook's lists arrive from the catalog.
        QTRY_VERIFY_WITH_TIMEOUT(itemsOf(table).contains("Orders") && itemsOf(query).contains("Totals"),10000);
        QCOMPARE(table->currentText(),QString("Orders"));QCOMPARE(query->currentText(),QString("Totals"));
        // The panel widens its dock rather than scroll sideways.
        auto *scroll=qobject_cast<QScrollArea *>(window.findChild<QDockWidget *>("chartDock")->widget());
        QTRY_VERIFY_WITH_TIMEOUT(scroll->widget()->width()<=scroll->viewport()->width(),5000);
        // Pick asks for a pick of its field and nothing else.
        auto *pick=window.findChild<QToolButton *>("series1ValuesPick");QVERIFY(pick);QCOMPARE(pick->text(),QString("Pick"));
        QCOMPARE(pick->accessibleName(),QString("Pick Series 1 Values on the grid"));
        QCOMPARE(window.findChild<QToolButton *>("categoriesSourcePick")->accessibleName(),QString("Pick Categories on the grid"));
        QSignalSpy picks(panelOf(window),&ChartPanel::pickRequested);press(pick);
        QCOMPARE(picks.count(),1);QCOMPARE(picks[0][0].toString(),QString("series1Values"));
        QTest::qWait(100);QCOMPARE(admitted.count(),0);
    }
    void typedRangeChangesTheValues() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Typed",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *values=rangeOf(window,"series1Values");QCOMPARE(values->text(),QString("B2:B4"));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        auto spec=[&]{return layer->chart(id)["spec"].toObject();};
        auto stored=[&]{return spec()["series"].toArray()[0].toObject()["values"].toObject()["source"].toObject();};
        typeInto(values,"B2:B3",true);
        QCOMPARE(requested.count(),1);QCOMPARE(requested[0][2].toString(),QString("series1Values"));QCOMPARE(admitted.count(),1);
        QCOMPARE(requested[0][1].toJsonObject().keys(),QStringList{"spec"});
        QTRY_COMPARE_WITH_TIMEOUT(stored()["ref"].toString(),QString("B2:B3"),10000);QCOMPARE(stored()["sheet_id"].toString(),model->sheet());
        QTRY_COMPARE_WITH_TIMEOUT(values->text(),QString("B2:B3"),5000);
        // Everything else is kept.
        QCOMPARE(spec()["categories"].toObject()["source"].toObject()["ref"].toString(),QString("A2:A4"));
        QCOMPARE(spec()["series"].toArray()[0].toObject()["name"].toObject()["source"].toObject()["ref"].toString(),QString("B1"));
        // The qualified form, applied on leaving the field.
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(values->isEnabled(),5000);
        typeInto(values,"Sheet1!$B$2:$B$4",false);QCOMPARE(requested.count(),1);rangeOf(window,"categoriesSource")->setFocus();
        QCOMPARE(requested.count(),2);
        QTRY_COMPARE_WITH_TIMEOUT(stored()["ref"].toString(),QString("B2:B4"),10000);QTRY_COMPARE_WITH_TIMEOUT(values->text(),QString("B2:B4"),5000);
        // The same range again sends nothing.
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(values->isEnabled(),5000);
        typeInto(values,"B2:B4",true);QTest::qWait(100);QCOMPARE(requested.count(),2);QCOMPARE(admitted.count(),2);
    }
    void tableColumnAndQueryColumnCanBeChosen() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addTableAndQuery(window,opened));QVERIFY(addChart(window,opened,"Chosen",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        auto stored=[&]{return layer->chart(id)["spec"].toObject()["series"].toArray()[0].toObject()["values"].toObject()["source"].toObject();};
        auto *table=window.findChild<QComboBox *>("series1ValuesTable"),*tableColumn=window.findChild<QComboBox *>("series1ValuesTableColumn");QVERIFY(table && tableColumn);
        QTRY_VERIFY_WITH_TIMEOUT(itemsOf(table).contains("Orders"),10000);
        // A kind, then a table, send nothing; the column completes the source.
        choose(kindOf(window,"series1Values"),"Table column");QCOMPARE(requested.count(),0);
        QVERIFY(table->isVisible());QVERIFY(!rangeOf(window,"series1Values")->isVisible());QCOMPARE(table->currentIndex(),-1);
        choose(table,"Orders");QCOMPARE(requested.count(),0);QCOMPARE(tableColumn->currentIndex(),-1);QCOMPARE(itemsOf(tableColumn),QStringList{"Quantity"});
        choose(tableColumn,"Quantity");QCOMPARE(requested.count(),1);QCOMPARE(admitted.count(),1);QCOMPARE(requested[0][2].toString(),QString("series1Values"));
        QTRY_COMPARE_WITH_TIMEOUT(stored()["kind"].toString(),QString("table"),10000);QVERIFY(idle(window));
        // Held by identity.
        auto *client=window.findChild<RestClient *>();const auto base="/v1/workbooks/"+model->workbook();
        const auto catalog=syncRequest(client,base+"/tables",{{"expected_state",model->state()}});QVERIFY2(!catalog.contains("_error"),qPrintable(catalog["_error"].toString()));
        const auto orders=catalog["tables"].toArray()[0].toObject();QCOMPARE(orders["name"].toString(),QString("Orders"));
        QCOMPARE(stored()["table_id"].toString(),orders["id"].toString());QCOMPARE(stored()["column_id"].toString(),orders["columns"].toArray()[0].toObject()["id"].toString());
        QCOMPARE(stored()["table"].toString(),QString("Orders"));QCOMPARE(stored()["column"].toString(),QString("Quantity"));QVERIFY(!stored()["broken"].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(table->currentText(),QString("Orders"),5000);QCOMPARE(tableColumn->currentText(),QString("Quantity"));
        // A query column the same way.
        QTRY_VERIFY_WITH_TIMEOUT(kindOf(window,"series1Values")->isEnabled(),5000);
        auto *query=window.findChild<QComboBox *>("series1ValuesQuery"),*queryColumn=window.findChild<QComboBox *>("series1ValuesQueryColumn");QVERIFY(query && queryColumn);
        choose(kindOf(window,"series1Values"),"Query column");QCOMPARE(requested.count(),1);QVERIFY(query->isVisible());QVERIFY(!table->isVisible());
        choose(query,"Totals");QCOMPARE(requested.count(),1);
        choose(queryColumn,"Total");QCOMPARE(requested.count(),2);QCOMPARE(admitted.count(),2);
        QTRY_COMPARE_WITH_TIMEOUT(stored()["kind"].toString(),QString("query"),10000);
        const auto totals=catalog["queries"].toArray()[0].toObject();QCOMPARE(totals["name"].toString(),QString("Totals"));
        QCOMPARE(stored()["query_id"].toString(),totals["id"].toString());QCOMPARE(stored()["column"].toString(),QString("Total"));QCOMPARE(stored()["query"].toString(),QString("Totals"));
        QTRY_COMPARE_WITH_TIMEOUT(query->currentText(),QString("Totals"),5000);QCOMPARE(queryColumn->currentText(),QString("Total"));
    }
    void seriesNameTakesTextOrOneCell() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Named",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *kind=kindOf(window,"series1Name");auto *name=rangeOf(window,"series1Name");auto *pick=window.findChild<QToolButton *>("series1NamePick");
        QCOMPARE(itemsOf(kind),(QStringList{"Text","Cell"}));QCOMPARE(kind->currentText(),QString("Cell"));QCOMPARE(name->text(),QString("B1"));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        auto stored=[&]{return layer->chart(id)["spec"].toObject()["series"].toArray()[0].toObject()["name"].toObject();};
        // Text: changing the kind clears the field and sends nothing.
        choose(kind,"Text");QCOMPARE(name->text(),QString());QVERIFY(!pick->isVisible());QCOMPARE(requested.count(),0);
        typeInto(name,"Units",true);QCOMPARE(requested.count(),1);QCOMPARE(requested[0][2].toString(),QString("series1Name"));
        QTRY_COMPARE_WITH_TIMEOUT(stored(),text("Units"),10000);QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(kind->isEnabled(),5000);
        QCOMPARE(kind->currentText(),QString("Text"));QCOMPARE(name->text(),QString("Units"));
        // Cell: one cell only; more is refused beside the field and the field goes back.
        choose(kind,"Cell");QVERIFY(pick->isVisible());QCOMPARE(name->text(),QString());
        typeInto(name,"B1:B2",true);
        auto *message=window.findChild<QLabel *>("series1NameMessage");QVERIFY(message);QVERIFY(message->isVisible());
        QCOMPARE(message->text(),QString("A series name takes one cell"));
        QCOMPARE(kind->currentText(),QString("Text"));QCOMPARE(name->text(),QString("Units"));
        QTest::qWait(100);QCOMPARE(requested.count(),1);QCOMPARE(admitted.count(),1);
        choose(kind,"Cell");typeInto(name,"B1",true);QCOMPARE(requested.count(),2);QVERIFY(!message->isVisible());
        QTRY_COMPARE_WITH_TIMEOUT(stored()["source"].toObject()["ref"].toString(),QString("B1"),10000);
        QCOMPARE(stored()["source"].toObject()["sheet_id"].toString(),model->sheet());
        QTRY_COMPARE_WITH_TIMEOUT(kind->currentText(),QString("Cell"),5000);QCOMPARE(name->text(),QString("B1"));
    }
    void addSeriesIsPanelOnlyUntilItsValuesAreGiven() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Grown",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *panel=panelOf(window);QSignalSpy requested(panel,&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        auto *add=window.findChild<QPushButton *>("addSeries");QVERIFY(add);QCOMPARE(add->text(),QString("Add Series"));
        QVERIFY(!window.findChild<QWidget *>("series2Values"));
        press(add);
        QVERIFY(window.findChild<QWidget *>("series2Values"));QVERIFY(rangeOf(window,"series2Values")->isVisible());
        QCOMPARE(rangeOf(window,"series2Values")->text(),QString());QCOMPARE(kindOf(window,"series2Name")->currentText(),QString("Text"));
        QCOMPARE(window.findChild<QLabel *>("series2ValuesMarker")->text(),QString("Not applied yet"));
        QTest::qWait(100);QCOMPARE(requested.count(),0);QCOMPARE(admitted.count(),0);
        // It survives a relist of the same, unchanged chart.
        window.findChild<QAction *>("refreshStoredDataAction")->trigger();QVERIFY(idle(window));QTest::qWait(200);
        QCOMPARE(panel->chartId(),id);QVERIFY(window.findChild<QWidget *>("series2Values"));QTRY_VERIFY_WITH_TIMEOUT(rangeOf(window,"series2Values")->isEnabled(),5000);
        // A name alone sends nothing; values complete it in one request.
        typeInto(rangeOf(window,"series2Name"),"Added",true);QTest::qWait(100);QCOMPARE(requested.count(),0);
        typeInto(rangeOf(window,"series2Values"),"B2:B3",true);
        QCOMPARE(requested.count(),1);QCOMPARE(admitted.count(),1);QCOMPARE(requested[0][2].toString(),QString("series2Values"));
        auto list=[&]{return layer->chart(id)["spec"].toObject()["series"].toArray();};
        QTRY_COMPARE_WITH_TIMEOUT(list().size(),2,10000);
        QCOMPARE(list()[1].toObject()["name"].toObject(),text("Added"));QCOMPARE(list()[1].toObject()["values"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B3"));
        QCOMPARE(list()[0].toObject()["values"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B4"));
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QLabel *>("series2ValuesMarker")->isHidden(),5000);
        // Another chart shown drops an unfilled row.
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(add->isEnabled(),5000);press(add);QVERIFY(window.findChild<QWidget *>("series3Values"));
        panel->setChart({});panel->setChart(layer->chart(id));
        QTRY_VERIFY_WITH_TIMEOUT(!window.findChild<QWidget *>("series3Values"),5000);QTest::qWait(100);QCOMPARE(requested.count(),1);
    }
    void removeAndReorderSeries() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        auto spec=chartSpec("Sheet1");QJsonArray three;
        for(const auto &[name,ref]:{std::pair{"A","B2:B4"},std::pair{"B","B2:B3"},std::pair{"C","B3:B4"}})
            three.append(QJsonObject{{"name",text(name)},{"values",QJsonObject{{"source",QJsonObject{{"kind","range"},{"sheet","Sheet1"},{"ref",ref}}}}}});
        spec["series"]=three;
        QVERIFY(addChart(window,opened,"Ordered",2,4,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        auto names=[&]{QStringList out;for(const auto &item:layer->chart(id)["spec"].toObject()["series"].toArray())out<<item.toObject()["name"].toObject()["text"].toString();return out;};
        auto button=[&](const QString &name){return window.findChild<QPushButton *>(name);};
        QCOMPARE(names(),(QStringList{"A","B","C"}));
        QCOMPARE(button("removeSeries1")->text(),QString("Remove"));QCOMPARE(button("seriesUp1")->text(),QString("Move Up"));QCOMPARE(button("seriesDown1")->text(),QString("Move Down"));
        QVERIFY(!button("seriesUp1")->isEnabled());QVERIFY(button("seriesDown1")->isEnabled());QVERIFY(button("seriesUp3")->isEnabled());QVERIFY(!button("seriesDown3")->isEnabled());
        press(button("seriesDown1"));QCOMPARE(requested.count(),1);QCOMPARE(admitted.count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(names(),(QStringList{"B","A","C"}),10000);QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(button("seriesUp3")->isEnabled(),5000);
        press(button("seriesUp3"));QCOMPARE(requested.count(),2);QCOMPARE(admitted.count(),2);
        QTRY_COMPARE_WITH_TIMEOUT(names(),(QStringList{"B","C","A"}),10000);QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(button("removeSeries2")->isEnabled(),5000);
        QCOMPARE(rangeOf(window,"series3Values")->text(),QString("B2:B4"));
        press(button("removeSeries2"));QCOMPARE(requested.count(),3);QCOMPARE(admitted.count(),3);
        QTRY_COMPARE_WITH_TIMEOUT(names(),(QStringList{"B","A"}),10000);
        QTRY_VERIFY_WITH_TIMEOUT(!window.findChild<QWidget *>("series3Values"),5000);
        const auto list=layer->chart(id)["spec"].toObject()["series"].toArray();
        QCOMPARE(list[1].toObject()["values"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B4"));
        QCOMPARE(list[0].toObject()["values"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B3"));
        QCOMPARE(window.findChild<QAction *>("undoAction")->text(),QString("Undo Edit chart"));
    }
    void lastSeriesCannotBeRemoved() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Single",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        auto *remove=window.findChild<QPushButton *>("removeSeries1");QVERIFY(remove);press(remove);
        auto *message=window.findChild<QLabel *>("seriesMessage");QVERIFY(message);QVERIFY(message->isVisible());
        QCOMPARE(message->text(),QString("A chart needs at least one series"));QVERIFY(window.findChild<QWidget *>("series1Values"));
        QTest::qWait(100);QCOMPARE(requested.count(),0);QCOMPARE(admitted.count(),0);
        QCOMPARE(layer->chart(id)["spec"].toObject()["series"].toArray().size(),1);
    }
    void badReferenceIsRefusedBesideTheField() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Strict",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        const auto before=layer->chart(id);
        auto *values=rangeOf(window,"series1Values");auto *message=window.findChild<QLabel *>("series1ValuesMessage");QVERIFY(message);
        for(const char *bad:{"Nope!B2:B4","Units","B2:"}) {
            typeInto(values,bad,true);
            QVERIFY2(message->isVisible(),bad);QCOMPARE(message->text(),QString("Enter a cell range such as A2:A13"));
            QCOMPARE(values->text(),QString("B2:B4"));QCOMPARE(values->accessibleDescription(),message->text());
        }
        typeInto(rangeOf(window,"categoriesSource"),"A2:A4,B2",true);
        QVERIFY(window.findChild<QLabel *>("categoriesSourceMessage")->isVisible());QCOMPARE(rangeOf(window,"categoriesSource")->text(),QString("A2:A4"));
        // Editing the field again clears its message.
        values->setFocus();QTest::keyClicks(values,"B");QVERIFY(!message->isVisible());QVERIFY(values->accessibleDescription().isEmpty());
        QTest::qWait(100);QCOMPARE(requested.count(),0);QCOMPARE(admitted.count(),0);QCOMPARE(layer->chart(id),before);
    }
    void sourceOverTheCellLimitShowsTheServiceReason() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(addChart(window,opened,"Limited",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        const auto before=layer->chart(id);QSignalSpy receipts(&window,&MainWindow::operationReceipt);
        auto *values=rangeOf(window,"series1Values");typeInto(values,"B2:B20000",true);
        QTRY_COMPARE_WITH_TIMEOUT(receipts.count(),1,10000);const auto receipt=receipts[0][0].toJsonObject();
        QVERIFY(receipt["outcome"]=="rejected" || receipt["outcome"]=="rolled_back");
        const auto serviceMessage=receipt["message"].toString();QVERIFY2(serviceMessage.contains("10000"),qPrintable(serviceMessage));
        auto *message=window.findChild<QLabel *>("series1ValuesMessage");QVERIFY(message);
        QTRY_VERIFY_WITH_TIMEOUT(message->isVisible(),5000);QCOMPARE(message->text(),serviceMessage);
        QCOMPARE(values->text(),QString("B2:B4"));QCOMPARE(values->accessibleDescription(),serviceMessage);
        QTest::qWait(200);QCOMPARE(layer->chart(id),before);
    }
    void brokenSourceIsRepairedWithOneUpdate() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));QVERIFY(applied(window,opened,{{"action","sheet_add"},{"name","Data"}}));
        QVERIFY(addChart(window,opened,"Mended",2,4,0,0,320,200,"Data"));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        auto *tabs=window.findChild<QTabBar *>("sheets");tabs->setCurrentIndex(1);QCOMPARE(tabs->tabText(1),QString("Data"));
        QVERIFY(applied(window,opened,{{"action","sheet_delete"}}));
        QTRY_VERIFY_WITH_TIMEOUT(overlayNamed(layer,"Mended") && ChartSpec::hasBrokenSource(layer->chart(overlayNamed(layer,"Mended")->chartId())),10000);
        const auto id=overlayNamed(layer,"Mended")->chartId();QVERIFY(idle(window));QVERIFY(openPanel(window,layer,id));activate(window);
        auto *reason=window.findChild<QLabel *>("chartPanelReason");QVERIFY(reason->isVisible());QCOMPARE(reason->text(),QString("A source of this chart no longer exists"));
        auto marker=[&](const QString &field){return window.findChild<QLabel *>(field+"Marker");};
        for(const char *field:{"categoriesSource","series1Name","series1Values"}) {
            QVERIFY2(marker(field) && marker(field)->isVisible(),field);QCOMPARE(marker(field)->text(),QString("This source no longer exists"));
            // A deleted sheet leaves no range to show.
            QVERIFY2(rangeOf(window,field)->isEnabled(),field);QVERIFY2(rangeOf(window,field)->text().isEmpty(),qPrintable(rangeOf(window,field)->text()));
        }
        QVERIFY(!window.findChild<QWidget *>("chartTitle")->isEnabled());QVERIFY(!window.findChild<QWidget *>("chartType_line")->isEnabled());
        QVERIFY(window.findChild<QWidget *>("addSeries")->isEnabled());QVERIFY(window.findChild<QWidget *>("chartName")->isEnabled());
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        // Each replacement is held, and marked, while a source is still broken.
        typeInto(rangeOf(window,"categoriesSource"),"A2:A4",true);
        QTest::qWait(100);QCOMPARE(requested.count(),0);QCOMPARE(marker("categoriesSource")->text(),QString("Not applied yet"));
        QCOMPARE(rangeOf(window,"categoriesSource")->text(),QString("A2:A4"));QCOMPARE(marker("series1Values")->text(),QString("This source no longer exists"));
        typeInto(rangeOf(window,"series1Values"),"B2:B4",true);
        QTest::qWait(100);QCOMPARE(requested.count(),0);QCOMPARE(marker("series1Values")->text(),QString("Not applied yet"));
        QCOMPARE(marker("series1Name")->text(),QString("This source no longer exists"));
        // The last one sends them all together.
        typeInto(rangeOf(window,"series1Name"),"B1",true);
        QCOMPARE(requested.count(),1);QCOMPARE(admitted.count(),1);
        QTRY_VERIFY_WITH_TIMEOUT(!ChartSpec::hasBrokenSource(layer->chart(id)),10000);
        const auto spec=layer->chart(id)["spec"].toObject();const auto first=spec["series"].toArray()[0].toObject();
        for(const auto &holder:{spec["categories"],first["name"],first["values"]})QCOMPARE(holder.toObject()["source"].toObject()["sheet_id"].toString(),model->sheet());
        QCOMPARE(spec["categories"].toObject()["source"].toObject()["ref"].toString(),QString("A2:A4"));
        QCOMPARE(first["name"].toObject()["source"].toObject()["ref"].toString(),QString("B1"));QCOMPARE(first["values"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B4"));
        QVERIFY(idle(window));QTest::qWait(200);QCOMPARE(admitted.count(),1);
        QTRY_VERIFY_WITH_TIMEOUT(!reason->isVisible(),5000);QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QWidget *>("chartTitle")->isEnabled(),5000);
        for(const char *field:{"categoriesSource","series1Name","series1Values"})QVERIFY2(marker(field)->isHidden(),field);
    }
    void scatterSeriesShowAnXField() {
        CHART_WINDOW;QVERIFY(fillChartData(window,opened));
        auto range=[](const QString &ref){return QJsonObject{{"source",QJsonObject{{"kind","range"},{"sheet","Sheet1"},{"ref",ref}}}};};
        const QJsonObject spec{{"spec_version",1},{"type","scatter"},{"series",QJsonArray{QJsonObject{{"name",range("B1")},{"values",range("B2:B4")},{"x",range("B2:B4")}}}}};
        QVERIFY(addChart(window,opened,"Scattered",2,4,0,0,320,200,"Sheet1",spec));QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);
        const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);
        auto *categories=window.findChild<QWidget *>("categoriesSource");QVERIFY(categories);QVERIFY(!categories->isVisible());
        auto *x=window.findChild<QWidget *>("series1X");QVERIFY(x);QVERIFY(x->isVisible());
        QCOMPARE(kindOf(window,"series1X")->currentText(),QString("Range"));QCOMPARE(rangeOf(window,"series1X")->text(),QString("B2:B4"));
        QCOMPARE(window.findChild<QToolButton *>("series1XPick")->accessibleName(),QString("Pick Series 1 X on the grid"));
        typeInto(rangeOf(window,"series1X"),"B2:B3",true);QCOMPARE(requested.count(),1);QCOMPARE(requested[0][2].toString(),QString("series1X"));
        auto list=[&]{return layer->chart(id)["spec"].toObject()["series"].toArray();};
        QTRY_COMPARE_WITH_TIMEOUT(list()[0].toObject()["x"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B3"),10000);
        // A new scatter series needs its X as well as its values.
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QWidget *>("addSeries")->isEnabled(),5000);
        press(window.findChild<QPushButton *>("addSeries"));QVERIFY(window.findChild<QWidget *>("series2X")->isVisible());
        typeInto(rangeOf(window,"series2Values"),"B2:B4",true);QTest::qWait(100);QCOMPARE(requested.count(),1);
        typeInto(rangeOf(window,"series2X"),"B2:B4",true);QCOMPARE(requested.count(),2);
        QTRY_COMPARE_WITH_TIMEOUT(list().size(),2,10000);QCOMPARE(list()[1].toObject()["x"].toObject()["source"].toObject()["ref"].toString(),QString("B2:B4"));
        // Another type shows Categories and no X.
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QWidget *>("chartType_line")->isEnabled(),5000);
        press(window.findChild<QToolButton *>("chartType_line"));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["type"].toString(),QString("line"),10000);
        QTRY_VERIFY_WITH_TIMEOUT(categories->isVisible(),5000);QVERIFY(!window.findChild<QWidget *>("series1X")->isVisible());
        QCOMPARE(rangeOf(window,"categoriesSource")->text(),QString("B2:B3"));
    }
#undef CHART_WINDOW

// A window attached to a service through ChartProxy, with a workbook open.
#define PROXY_WINDOW \
    QTemporaryDir temp;QJsonObject service;StartupOptions ownerOptions;ownerOptions.automaticWorkbook=false;ownerOptions.viewStatePath=temp.filePath("owner.ini"); \
    MainWindow owner(ownerOptions);observeOwnedServiceStartup(owner,service);QSignalSpy ownerReady(&owner,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ownerReady.count(),1,10000);QVERIFY(!service["token"].toString().isEmpty()); \
    ChartProxy proxy(service);QVERIFY(proxy.listen(QHostAddress::LocalHost,0)); \
    StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");options.url="http://127.0.0.1:"+QString::number(proxy.serverPort()); \
    options.token=service["token"].toString();options.recoveryDirectory=temp.filePath("recovery"); \
    MainWindow window(options);window.resize(1200,800);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000); \
    QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("charts.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000); \
    auto *layer=window.findChild<ChartLayer *>("chartLayer");QVERIFY(layer);auto *grid=window.findChild<QTableView *>("grid");QVERIFY(grid);auto *model=window.model()
    void insertWhileBusyShowsTheReason() {
        PROXY_WINDOW;QVERIFY(fillChartData(window,opened));
        selectCells(grid,model,0,0,3,1);
        auto *insert=window.findChild<QAction *>("insertChartAction");QVERIFY(insert->isEnabled());
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        proxy.holdSuggest=true;insert->trigger();QTRY_COMPARE_WITH_TIMEOUT(proxy.suggests,1,10000);
        // Disabled while the suggestion is awaited; a second Insert sends nothing.
        QVERIFY(!insert->isEnabled());window.insertChart();QTest::qWait(100);QCOMPARE(proxy.suggests,1);
        // The window becomes busy (a refresh, which keeps the state) before the answer.
        proxy.holdMetadata=true;window.findChild<QAction *>("refreshStoredDataAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(proxy.heldMetadata(),1,10000);
        proxy.releaseSuggest();
        QTRY_VERIFY_WITH_TIMEOUT(issuesText(window).contains("The chart was not inserted • Insert again"),10000);
        proxy.releaseMetadata();QTRY_VERIFY_WITH_TIMEOUT(insert->isEnabled(),10000);
        QTest::qWait(200);QCOMPARE(admitted.count(),0);QCOMPARE(layer->overlays().size(),0);
    }
    void redoDoesNotReselectAnInsertedChart() {
        PROXY_WINDOW;QVERIFY(fillChartData(window,opened));
        selectCells(grid,model,0,0,3,1);
        QSignalSpy finished(&window,&MainWindow::operationFinished);
        auto *undo=window.findChild<QAction *>("undoAction"),*redo=window.findChild<QAction *>("redoAction");
        // The catalog read after the create is held, so Undo comes before the layer lists the chart.
        const int before=opened.count();proxy.holdCharts=true;window.findChild<QAction *>("insertChartAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(finished.count(),1,10000);QVERIFY(finished[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);QVERIFY(undo->isEnabled());QCOMPARE(layer->overlays().size(),0);undo->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(finished.count(),2,10000);QVERIFY(finished[1][0].toBool());
        proxy.releaseCharts();QTest::qWait(300);QCOMPARE(layer->overlays().size(),0);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+2,10000);QVERIFY(redo->isEnabled());redo->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(finished.count(),3,10000);QVERIFY(finished[2][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,10000);QTest::qWait(300);
        QCOMPARE(layer->selectedChart(),QString());QVERIFY(!layer->overlays().first()->selected());
        QVERIFY(statusText(window)!="Chart inserted • Undo removes it");
    }
    void aLostCreateAnswerStillSelectsTheChart() {
        PROXY_WINDOW;QVERIFY(fillChartData(window,opened));
        selectCells(grid,model,0,0,3,1);
        QSignalSpy receipts(&window,&MainWindow::operationReceipt);
        proxy.loseCreate=true;window.findChild<QAction *>("insertChartAction")->trigger();
        // The answer is lost: an unknown receipt, then reconcile's committed one.
        QTRY_VERIFY_WITH_TIMEOUT(receipts.count()>=2,15000);
        QCOMPARE(receipts[0][0].toJsonObject()["outcome"].toString(),QString("unknown"));
        QCOMPARE(receipts[1][0].toJsonObject()["outcome"].toString(),QString("committed"));
        QTRY_VERIFY_WITH_TIMEOUT(!layer->selectedChart().isEmpty(),10000);
        QCOMPARE(layer->selectedChart(),receipts[1][0].toJsonObject()["chart_id"].toString());
        QCOMPARE(layer->overlays().size(),1);QCOMPARE(statusText(window),QString("Chart inserted • Undo removes it"));
    }
    void nothingMovesWhileBusyOrReadOnly() {
        PROXY_WINDOW;Q_UNUSED(model);QVERIFY(addChart(window,opened,"Fixed",2,4,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);QTRY_VERIFY_WITH_TIMEOUT(layer->overlays().first()->isVisible(),5000);
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        QSignalSpy moved(layer,&ChartLayer::moveRequested),resized(layer,&ChartLayer::resizeRequested),deleted(layer,&ChartLayer::deleteRequested),ordered(layer,&ChartLayer::orderRequested);
        auto *menu=window.findChild<QMenu *>("chartContextMenu");QVERIFY(menu);
        auto attempt=[&](const QString &phase) {
            auto *overlay=layer->overlays().first();const auto id=overlay->chartId();const QRect before=overlay->geometry();
            // A click still selects.
            QTest::mouseClick(overlay,Qt::LeftButton,Qt::NoModifier,QPoint(60,60));
            QVERIFY2(layer->selectedChart()==id,qPrintable(phase));QCOMPARE(window.focusWidget(),overlay);
            // Neither the chart nor a handle drags.
            for(const QPoint &at:{QPoint(60,60),QPoint(overlay->width()-4,overlay->height()-4)}) {
                const QPoint end=dragBy(overlay,at,QPoint(80,40),false);QVERIFY2(overlay->geometry()==before,qPrintable(phase));
                QTest::mouseRelease(overlay,Qt::LeftButton,Qt::NoModifier,overlay->mapFromGlobal(end));QVERIFY2(overlay->geometry()==before,qPrintable(phase));
            }
            // The keys are not taken.
            for(const auto modifiers:{Qt::KeyboardModifiers(),Qt::KeyboardModifiers(Qt::ShiftModifier)})
                for(const auto key:{Qt::Key_Right,Qt::Key_Down,Qt::Key_Delete,Qt::Key_Backspace}) {
                    QKeyEvent override(QEvent::ShortcutOverride,key,modifiers);override.ignore();
                    QCoreApplication::sendEvent(overlay,&override);QVERIFY2(!override.isAccepted(),qPrintable(phase));
                    QTest::keyClick(overlay,key,modifiers);
                }
            QVERIFY2(overlay->geometry()==before,qPrintable(phase));QCOMPARE(layer->overlays().size(),1);QCOMPARE(layer->selectedChart(),id);
            // The menu offers Edit Chart only.
            QTest::mouseClick(overlay,Qt::RightButton,Qt::NoModifier,QPoint(30,30));QTRY_VERIFY_WITH_TIMEOUT(menu->isVisible(),5000);
            QVERIFY(window.findChild<QAction *>("editChartAction")->isEnabled());
            for(const char *name:{"chartToFrontAction","chartToBackAction","deleteChartAction"})QVERIFY2(!window.findChild<QAction *>(name)->isEnabled(),name);
            menu->hide();
            QCOMPARE(moved.count()+resized.count()+deleted.count()+ordered.count(),0);
        };
        // Busy: an operation is in flight.
        proxy.holdOperations=true;
        QVERIFY(!window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",20},{"col",1},{"source","9"}}}}}).isEmpty());
        attempt("busy");if(QTest::currentTestFailed())return;
        proxy.releaseOperations();QVERIFY(idle(window));QCOMPARE(admitted.count(),1);
        // Read-only.
        const int before=opened.count();window.openBook(temp.filePath("charts.dhwb"),false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);QTRY_VERIFY_WITH_TIMEOUT(layer->overlays().first()->isVisible(),5000);
        attempt("read-only");if(QTest::currentTestFailed())return;
        QTest::qWait(200);QCOMPARE(admitted.count(),1);
        const auto chart=layer->chart(layer->overlays().first()->chartId());
        QCOMPARE(chart["anchor"].toObject(),(QJsonObject{{"row",2},{"col",4}}));QCOMPARE(chart["size"].toObject(),(QJsonObject{{"width",320},{"height",200}}));
    }
    void refusedChangeGoesBack() {
        PROXY_WINDOW;Q_UNUSED(model);QVERIFY(addChart(window,opened,"Refused",3,3,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto *overlay=layer->overlays().first();QTRY_VERIFY_WITH_TIMEOUT(overlay->isVisible(),5000);
        const auto id=overlay->chartId();const QRect before=overlay->geometry();
        QSignalSpy receipts(&window,&MainWindow::operationReceipt);proxy.refuseChartUpdates=true;
        // A refused move: the chart returns to its stored rectangle and the service's message is reported.
        dragBy(overlay,QPoint(60,60),QPoint(90,50));
        QTRY_COMPARE_WITH_TIMEOUT(receipts.count(),1,10000);QCOMPARE(receipts[0][0].toJsonObject()["outcome"].toString(),QString("rejected"));
        QTRY_COMPARE_WITH_TIMEOUT(overlay->geometry(),before,5000);QCOMPARE(overlay->mask(),QRegion(overlay->rect()));
        QVERIFY2(issuesText(window).contains("The test proxy refused this chart change"),qPrintable(issuesText(window)));
        QCOMPARE(layer->chart(id)["anchor"].toObject(),(QJsonObject{{"row",3},{"col",3}}));QCOMPARE(layer->selectedChart(),id);
        // A refused mouse resize the same.
        QVERIFY(idle(window));
        dragBy(overlay,QPoint(overlay->width()-4,overlay->height()-4),QPoint(30,20));
        QTRY_COMPARE_WITH_TIMEOUT(receipts.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(overlay->geometry(),before,5000);
        // Once accepted, a move is stored.
        QVERIFY(idle(window));proxy.refuseChartUpdates=false;
        dragBy(overlay,QPoint(60,60),QPoint(90,50));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlay(id)->geometry(),before.translated(90,50),10000);
    }
    void relistDuringADragKeepsTheDraggedChart() {
        PROXY_WINDOW;Q_UNUSED(model);QVERIFY(addChart(window,opened,"Held",3,3,0,0,320,200));QVERIFY(addChart(window,opened,"Other",16,3,0,0,200,150));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);auto *held=overlayNamed(layer,"Held");QTRY_VERIFY_WITH_TIMEOUT(held->isVisible(),5000);
        const auto id=held->chartId();auto *area=held->parentWidget();
        // The catalog read after a change elsewhere is held until the drag is under way.
        proxy.holdCharts=true;QVERIFY(!window.submit({{"action","chart_update"},{"chart_id",overlayNamed(layer,"Other")->chartId()},{"name","Renamed"}}).isEmpty());
        QVERIFY(idle(window));
        const QRect before=held->geometry();const QPoint delta(80,40);
        const QPoint end=dragBy(held,QPoint(60,60),delta,false);QCOMPARE(held->geometry(),before.translated(delta));
        proxy.releaseCharts();QTRY_VERIFY_WITH_TIMEOUT(overlayNamed(layer,"Renamed"),10000);
        QCOMPARE(layer->overlay(id),held);QCOMPARE(held->geometry(),before.translated(delta));
        layer->relayout();QCOMPARE(held->geometry(),before.translated(delta));QCOMPARE(held->mask(),QRegion(held->rect()));
        // The drag still ends in one move.
        QSignalSpy moved(layer,&ChartLayer::moveRequested);
        QTest::mouseRelease(held,Qt::LeftButton,Qt::NoModifier,held->mapFromGlobal(end));QCOMPARE(moved.count(),1);
        QJsonObject cell,inside;QVERIFY(cellUnder(grid,area,before.topLeft()+delta,&cell,&inside));
        QTRY_VERIFY_WITH_TIMEOUT(layer->chart(id)["anchor"].toObject()==cell && layer->chart(id)["offset"].toObject()==inside,10000);
    }
    void chartDeletedDuringADragEndsItSilently() {
        PROXY_WINDOW;Q_UNUSED(model);QVERIFY(addChart(window,opened,"First",3,3,0,0,320,200));QVERIFY(addChart(window,opened,"Second",16,3,0,0,320,200));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        QPointer<ChartOverlay> first=overlayNamed(layer,"First"),second=overlayNamed(layer,"Second");QTRY_VERIFY_WITH_TIMEOUT(first->isVisible() && second->isVisible(),5000);
        QSignalSpy admitted(&window,&MainWindow::operationAdmitted);QSignalSpy moved(layer,&ChartLayer::moveRequested);
        // Deleted through submit while the button is held: the drag ends at once.
        QPoint end=dragBy(first,QPoint(60,60),QPoint(80,40),false);
        QVERIFY(!window.submit({{"action","chart_delete"},{"chart_id",first->chartId()}}).isEmpty());
        QTRY_VERIFY_WITH_TIMEOUT(!first,10000);
        QTest::mouseRelease(grid->viewport(),Qt::LeftButton,Qt::NoModifier,grid->viewport()->mapFromGlobal(end));
        QVERIFY(idle(window));QCOMPARE(admitted.count(),1);
        // Deleted while the drag is under way (its catalog read held until then): the relist ends it silently.
        proxy.holdCharts=true;QVERIFY(!window.submit({{"action","chart_delete"},{"chart_id",second->chartId()}}).isEmpty());
        QVERIFY(idle(window));QVERIFY(second);
        end=dragBy(second,QPoint(60,60),QPoint(80,40),false);
        proxy.releaseCharts();QTRY_VERIFY_WITH_TIMEOUT(!second,10000);QCOMPARE(layer->overlays().size(),0);
        QTest::mouseRelease(grid->viewport(),Qt::LeftButton,Qt::NoModifier,grid->viewport()->mapFromGlobal(end));
        QTest::qWait(200);QCOMPARE(admitted.count(),2);QCOMPARE(moved.count(),0);
    }
    void unchangedTextSendsNothing() {
        PROXY_WINDOW;Q_UNUSED(grid);Q_UNUSED(model);
        auto stored=chartSpec("Sheet1");stored["title"]=QJsonObject{{"text","Kept"}};
        stored["axes"]=QJsonObject{{"category",QJsonObject{{"title",QJsonValue::Null}}},{"value",QJsonObject{{"title",QJsonValue::Null},{"min",5},{"max",QJsonValue::Null}}}};
        QVERIFY(addChart(window,opened,"Same",2,4,0,0,320,200,"Sheet1",stored));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *panel=panelOf(window);QSignalSpy requested(panel,&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        auto *name=window.findChild<QLineEdit *>("chartName");
        // Enter and focus-out on unchanged fields, and the same text typed again.
        QCOMPARE(window.focusWidget(),static_cast<QWidget *>(name));QTest::keyClick(name,Qt::Key_Return);
        for(const char *field:{"chartTitle","categoryAxisTitle","valueAxisTitle","valueAxisMin","valueAxisMax","chartName"})window.findChild<QLineEdit *>(field)->setFocus();
        typeInto(name,"Same",true);window.findChild<QLineEdit *>("chartTitle")->setFocus();
        // The same title, and "5.0" against a stored minimum of 5, retyped and left.
        typeInto(window.findChild<QLineEdit *>("chartTitle"),"Kept",false);
        typeInto(window.findChild<QLineEdit *>("valueAxisMin"),"5.0",false);name->setFocus();
        QTest::qWait(100);QCOMPARE(requested.count(),0);QCOMPARE(admitted.count(),0);
        QCOMPARE(window.findChild<QLineEdit *>("valueAxisMin")->text(),QString("5.0"));
        // A second change while the first is in flight sends nothing, even with the panel unlocked.
        proxy.holdOperations=true;
        typeInto(name,"Once",true);QCOMPARE(requested.count(),1);QCOMPARE(admitted.count(),1);
        panel->setLocked(false);
        typeInto(window.findChild<QLineEdit *>("chartTitle"),"Twice",true);
        QTest::mouseClick(window.findChild<QToolButton *>("chartType_line"),Qt::LeftButton);
        auto *legend=window.findChild<QComboBox *>("legendPosition");legend->setFocus();QTest::keyClicks(legend,"Bottom");
        QTest::mouseClick(window.findChild<QPushButton *>("chartToBack"),Qt::LeftButton);
        QCOMPARE(requested.count(),1);
        proxy.releaseOperations();
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["name"].toString(),QString("Once"),10000);QVERIFY(idle(window));
        QTest::qWait(200);QCOMPARE(admitted.count(),1);
        const auto spec=layer->chart(id)["spec"].toObject();
        QCOMPARE(spec["title"].toObject()["text"].toString(),QString("Kept"));QCOMPARE(spec["type"].toString(),QString("column"));QCOMPARE(spec["legend"].toObject()["position"].toString(),QString("right"));
        // The reload (the chart changed) put the unsent text back.
        QTRY_COMPARE_WITH_TIMEOUT(window.findChild<QLineEdit *>("chartTitle")->text(),QString("Kept"),5000);
        QVERIFY(window.findChild<QToolButton *>("chartType_column")->isChecked());
        // After the answer the panel sends again.
        typeInto(name,"Again",true);QCOMPARE(requested.count(),2);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["name"].toString(),QString("Again"),10000);
    }
    void panelIsLockedWhileBusyAndWhenReadOnly() {
        PROXY_WINDOW;Q_UNUSED(grid);Q_UNUSED(model);QVERIFY(addChart(window,opened,"Locked",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        const QStringList fields{"chartName","chartToFront","chartToBack","chartType_column","chartType_bar","chartType_line","chartType_area","chartType_pie","chartType_scatter",
                                 "chartGrouping","chartTitle","categoryAxisTitle","valueAxisTitle","valueAxisMin","valueAxisMax","legendPosition"};
        auto enabled=[&](bool expected){for(const auto &field:fields)if(window.findChild<QWidget *>(field)->isEnabled()!=expected)return field;return QString();};
        QCOMPARE(enabled(true),QString());
        // Busy: every control disabled, values kept.
        proxy.holdOperations=true;
        QVERIFY(!window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",20},{"col",1},{"source","9"}}}}}).isEmpty());
        QCOMPARE(enabled(false),QString());QCOMPARE(window.findChild<QLineEdit *>("chartName")->text(),QString("Locked"));
        QVERIFY(window.findChild<QToolButton *>("chartType_column")->isChecked());QCOMPARE(window.findChild<QComboBox *>("legendPosition")->currentText(),QString("Right"));
        proxy.releaseOperations();QVERIFY(idle(window));QTRY_COMPARE_WITH_TIMEOUT(enabled(true),QString(),5000);
        // Grouping stays disabled for a type that takes none.
        auto *grouping=window.findChild<QComboBox *>("chartGrouping");QVERIFY(grouping->isEnabled());
        QTest::mouseClick(window.findChild<QToolButton *>("chartType_line"),Qt::LeftButton);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["type"].toString(),QString("line"),10000);QVERIFY(idle(window));
        QTRY_VERIFY_WITH_TIMEOUT(!grouping->isEnabled(),5000);QVERIFY(window.findChild<QWidget *>("chartTitle")->isEnabled());
        // Read-only: Edit Chart opens the panel locked.
        auto *dock=window.findChild<QDockWidget *>("chartDock");dock->hide();
        const int before=opened.count();window.openBook(temp.filePath("charts.dhwb"),false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),before+1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);id=layer->overlays().first()->chartId();
        layer->select(id);window.findChild<QAction *>("editChartAction")->trigger();QVERIFY(dock->isVisible());QCOMPARE(panelOf(window)->chartId(),id);
        QCOMPARE(enabled(false),QString());QCOMPARE(window.findChild<QLineEdit *>("chartName")->text(),QString("Locked"));
        QVERIFY(window.findChild<QToolButton *>("chartType_line")->isChecked());
    }
    void heldTextIsAppliedOnUnlock() {
        PROXY_WINDOW;Q_UNUSED(grid);Q_UNUSED(model);QVERIFY(addChart(window,opened,"Held",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        QSignalSpy requested(panelOf(window),&ChartPanel::changeRequested);
        auto *title=window.findChild<QLineEdit *>("chartTitle");title->setFocus();QTest::keyClicks(title,"Typed before");
        // An unrelated operation locks the panel while the text is unsent.
        proxy.holdOperations=true;
        QVERIFY(!window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",20},{"col",1},{"source","9"}}}}}).isEmpty());
        QVERIFY(!title->isEnabled());QCOMPARE(title->text(),QString("Typed before"));QCOMPARE(requested.count(),0);
        proxy.releaseOperations();
        // Once unlocked on the same chart, the held text is sent.
        QTRY_COMPARE_WITH_TIMEOUT(requested.count(),1,10000);QCOMPARE(requested[0][2].toString(),QString("chartTitle"));
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["title"].toObject()["text"].toString(),QString("Typed before"),10000);
        // When the chart itself changes meanwhile, the held text gives way to it.
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(title->isEnabled(),5000);
        title->setFocus();title->selectAll();QTest::keyClicks(title,"Dropped");
        proxy.holdOperations=true;
        QVERIFY(!window.submit({{"action","chart_update"},{"chart_id",id},{"name","Renamed elsewhere"}}).isEmpty());
        proxy.releaseOperations();
        QTRY_COMPARE_WITH_TIMEOUT(window.findChild<QLineEdit *>("chartName")->text(),QString("Renamed elsewhere"),10000);
        QVERIFY(idle(window));QTest::qWait(200);
        QCOMPARE(requested.count(),1);QCOMPARE(title->text(),QString("Typed before"));
        QCOMPARE(layer->chart(id)["spec"].toObject()["title"].toObject()["text"].toString(),QString("Typed before"));
    }
    void failedCatalogReadShowsNoChartUntilTheNextListing() {
        PROXY_WINDOW;Q_UNUSED(grid);Q_UNUSED(model);QVERIFY(addChart(window,opened,"Read",2,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *panel=panelOf(window);QSignalSpy requested(panel,&ChartPanel::changeRequested);
        proxy.failCharts=true;typeInto(window.findChild<QLineEdit *>("chartName"),"Committed",true);QCOMPARE(requested.count(),1);
        QTRY_VERIFY_WITH_TIMEOUT(issuesText(window).contains("The test proxy failed this chart read"),10000);
        QTRY_VERIFY_WITH_TIMEOUT(panel->chartId().isEmpty() && noChartLabel(panel)->isVisible(),5000);
        // The next listing shows the chart as stored, and the panel asks again.
        QVERIFY(idle(window));window.findChild<QAction *>("refreshStoredDataAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(panel->chartId(),id,10000);QCOMPARE(window.findChild<QLineEdit *>("chartName")->text(),QString("Committed"));
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QWidget *>("legendPosition")->isEnabled(),5000);
        choose(window.findChild<QComboBox *>("legendPosition"),"Bottom");QCOMPARE(requested.count(),2);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["legend"].toObject()["position"].toString(),QString("bottom"),10000);
        QCOMPARE(layer->chart(id)["name"].toString(),QString("Committed"));
    }
    void reselectAfterAFailedReadShowsNoChart() {
        PROXY_WINDOW;Q_UNUSED(grid);Q_UNUSED(model);QVERIFY(addChart(window,opened,"First",2,4));QVERIFY(addChart(window,opened,"Second",14,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        const auto first=overlayNamed(layer,"First")->chartId(),second=overlayNamed(layer,"Second")->chartId();QVERIFY(openPanel(window,layer,first));
        auto *panel=panelOf(window);QSignalSpy requested(panel,&ChartPanel::changeRequested);QSignalSpy admitted(&window,&MainWindow::operationAdmitted);
        proxy.failCharts=true;typeInto(window.findChild<QLineEdit *>("chartName"),"Committed",true);QCOMPARE(requested.count(),1);
        QTRY_VERIFY_WITH_TIMEOUT(issuesText(window).contains("The test proxy failed this chart read"),10000);QVERIFY(idle(window));
        QTRY_VERIFY_WITH_TIMEOUT(panel->chartId().isEmpty(),5000);
        // The layer still holds the catalog from before the change; selecting again shows no chart from it.
        layer->select(second);QCOMPARE(layer->selectedChart(),second);QVERIFY(panel->chartId().isEmpty());QVERIFY(noChartLabel(panel)->isVisible());
        layer->select(first);QVERIFY(panel->chartId().isEmpty());QVERIFY(noChartLabel(panel)->isVisible());
        window.findChild<QAction *>("selectNextChartAction")->trigger();QVERIFY(panel->chartId().isEmpty());
        window.findChild<QAction *>("editChartAction")->trigger();QVERIFY(panel->chartId().isEmpty());
        // An attempted edit sends nothing.
        auto *name=window.findChild<QLineEdit *>("chartName");QVERIFY(!name->isEnabled());
        panel->setLocked(false);name->setFocus();QTest::keyClicks(name," again");QTest::keyClick(name,Qt::Key_Return);
        auto *legend=window.findChild<QComboBox *>("legendPosition");QTest::keyClicks(legend,"Bottom");
        QTest::qWait(200);QCOMPARE(requested.count(),1);QCOMPARE(admitted.count(),1);
        // A successful read restores the panel on the stored chart.
        layer->select(first);window.findChild<QAction *>("refreshStoredDataAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(panel->chartId(),first,10000);QCOMPARE(name->text(),QString("Committed"));
        QVERIFY(idle(window));QTRY_VERIFY_WITH_TIMEOUT(legend->isEnabled(),5000);
        choose(legend,"Bottom");QCOMPARE(requested.count(),2);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(first)["spec"].toObject()["legend"].toObject()["position"].toString(),QString("bottom"),10000);
        QCOMPARE(layer->chart(first)["name"].toString(),QString("Committed"));
    }
    void focusStaysWhereThePersonWentDuringTheWait() {
        PROXY_WINDOW;Q_UNUSED(model);QVERIFY(addChart(window,opened,"Waiting",2,4));activate(window);
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),1,5000);const auto id=layer->overlays().first()->chartId();QVERIFY(openPanel(window,layer,id));
        auto *title=window.findChild<QLineEdit *>("chartTitle");title->setFocus();QTest::keyClicks(title,"Moved on");
        // The catalog read after the change is held. Disabling the field left
        // no focus widget; meanwhile the person presses an arrow, which the
        // window takes as no widget has focus.
        proxy.holdCharts=true;QTest::keyClick(title,Qt::Key_Return);
        QTRY_VERIFY_WITH_TIMEOUT(idle(window),10000);QVERIFY(!title->isEnabled());
        auto *after=window.focusWidget();QVERIFY(after!=title);
        QTest::keyClick(after?after:static_cast<QWidget *>(&window),Qt::Key_Down);
        proxy.releaseCharts();
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["title"].toObject()["text"].toString(),QString("Moved on"),10000);
        QTRY_VERIFY_WITH_TIMEOUT(title->isEnabled(),5000);QTest::qWait(200);
        QCOMPARE(window.focusWidget(),after);
        // A person who went to the grid and used the arrows there keeps the grid.
        QVERIFY(idle(window));title->setFocus();QTest::keyClicks(title," again");
        proxy.holdCharts=true;QTest::keyClick(title,Qt::Key_Return);QTRY_VERIFY_WITH_TIMEOUT(idle(window),10000);
        grid->setFocus();QTest::keyClick(grid,Qt::Key_Down);
        proxy.releaseCharts();
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(id)["spec"].toObject()["title"].toObject()["text"].toString(),QString("Moved on again"),10000);
        QTRY_VERIFY_WITH_TIMEOUT(title->isEnabled(),5000);QTest::qWait(200);
        QCOMPARE(window.focusWidget(),static_cast<QWidget *>(grid));
    }
    void refusalForAnotherChartStaysOffThePanel() {
        PROXY_WINDOW;Q_UNUSED(grid);Q_UNUSED(model);QVERIFY(addChart(window,opened,"First",2,4));QVERIFY(addChart(window,opened,"Second",14,4));
        QTRY_COMPARE_WITH_TIMEOUT(layer->overlays().size(),2,5000);
        const auto first=overlayNamed(layer,"First")->chartId(),second=overlayNamed(layer,"Second")->chartId();QVERIFY(openPanel(window,layer,first));
        auto *panel=panelOf(window);QSignalSpy requested(panel,&ChartPanel::changeRequested);QSignalSpy receipts(&window,&MainWindow::operationReceipt);
        proxy.refuseChartUpdates=true;proxy.holdOperations=true;
        typeInto(window.findChild<QLineEdit *>("chartTitle"),"Refused",true);QCOMPARE(requested.count(),1);
        // Another chart is selected, then the first again, before the answer: nothing more is asked.
        layer->select(second);QCOMPARE(panel->chartId(),second);layer->select(first);QCOMPARE(panel->chartId(),first);
        panel->setLocked(false);auto *legend=window.findChild<QComboBox *>("legendPosition");legend->setFocus();QTest::keyClicks(legend,"Bottom");
        QCOMPARE(requested.count(),1);QCOMPARE(legend->currentText(),QString("Right"));
        layer->select(second);QCOMPARE(panel->chartId(),second);
        proxy.releaseOperations();QTRY_COMPARE_WITH_TIMEOUT(receipts.count(),1,10000);
        QVERIFY(issuesText(window).contains("The test proxy refused this chart change"));
        // The message is not put beside the second chart's field.
        QVERIFY(window.findChild<QLabel *>("chartTitleMessage")->isHidden());
        // The wait is over: the second chart can be changed.
        proxy.refuseChartUpdates=false;QVERIFY(idle(window));
        typeInto(window.findChild<QLineEdit *>("chartTitle"),"Accepted",true);QCOMPARE(requested.count(),2);QCOMPARE(requested[1][0].toString(),second);
        QTRY_COMPARE_WITH_TIMEOUT(layer->chart(second)["spec"].toObject()["title"].toObject()["text"].toString(),QString("Accepted"),10000);
        QVERIFY(layer->chart(first)["spec"].toObject()["title"].toObject()["text"].isNull());
    }
#undef PROXY_WINDOW

private:
    static void observeOwnedServiceStartup(MainWindow &owner,QJsonObject &service) {
        auto *process=owner.findChild<QProcess *>("ownedService");
        connect(process,&QProcess::readyReadStandardOutput,process,[process,&service,bytes=QByteArray{}]() mutable {
            if(!service.isEmpty())return;
            bytes+=process->peek(process->bytesAvailable());
            const auto newline=bytes.indexOf('\n');
            if(newline>=0)service=QJsonDocument::fromJson(bytes.left(newline)).object();
        });
    }
    // Labels in A2:A4, a series name in B1 and values in B2:B4, as chartSpec reads them.
    static bool fillChartData(MainWindow &window,QSignalSpy &opened) {
        const auto before=opened.count();QJsonArray cells{QJsonObject{{"row",1},{"col",2},{"source","Sales"}}};
        for(int row=2;row<=4;++row){cells.append(QJsonObject{{"row",row},{"col",1},{"source","Item "+QString::number(row)}});cells.append(QJsonObject{{"row",row},{"col",2},{"source",QString::number(row*100)}});}
        window.submit({{"action","edit"},{"cells",cells}});
        for(int i=0;i<1000 && opened.count()==before;++i)QTest::qWait(10);
        return opened.count()>before;
    }
    // Submits an operation and waits for the window to accept the new state.
    static bool applied(MainWindow &window,QSignalSpy &opened,const QJsonObject &operation) {
        const auto before=opened.count();
        if(window.submit(operation).isEmpty())return false;
        for(int i=0;i<1000 && opened.count()==before;++i)QTest::qWait(10);
        return opened.count()>before;
    }
    static QJsonObject syncRequest(RestClient *client,const QString &path,const QJsonObject &body) {
        auto response=std::make_shared<QJsonObject>();auto done=std::make_shared<bool>(false);
        client->request(path,body,[response,done](QJsonObject result,QString error){*response=result;if(!error.isEmpty())(*response)["_error"]=error;*done=true;});
        for(int i=0;i<500 && !*done;++i)QTest::qWait(10);
        if(!*done)(*response)["_error"]="timed out";
        return *response;
    }
    // A table Orders (Quantity 1, 2, 3) and a saved query Totals (Total) over it.
    static bool addTableAndQuery(MainWindow &window,QSignalSpy &opened) {
        if(!applied(window,opened,{{"action","table_create"},{"name","Orders"},{"columns",QJsonArray{QJsonObject{{"name","Quantity"},{"type","integer"}}}}}))return false;
        if(!applied(window,opened,{{"action","table_insert"},{"table","Orders"},{"rows",QJsonArray{QJsonObject{{"Quantity",1}},QJsonObject{{"Quantity",2}},QJsonObject{{"Quantity",3}}}}}))return false;
        const auto review=syncRequest(window.findChild<RestClient *>(),"/v1/workbooks/"+window.model()->workbook()+"/queries/preview",
                                      {{"name","Totals"},{"sql","SELECT Quantity AS Total FROM Orders"},{"expected_state",window.model()->state()}});
        if(review.contains("_error"))return false;
        auto save=review["source_request"].toObject();save["action"]="query_save";save["review_digest"]=review["review_digest"];
        if(!applied(window,opened,save))return false;
        // Creating a table showed Tables and queries; back to the sheets.
        if(auto *back=window.findChild<QPushButton *>("returnToSheets");back && back->isVisible())back->click();
        return true;
    }
    // Scrolls the panel to a control and clicks it, as a person would.
    static void press(QWidget *control) {
        // Laid out first, as a person would see it before clicking.
        QTest::qWait(50);
        for(auto *parent=control->parentWidget();parent;parent=parent->parentWidget())
            if(auto *scroll=qobject_cast<QScrollArea *>(parent)){scroll->ensureWidgetVisible(control);break;}
        QTest::mouseClick(control,Qt::LeftButton);
    }
    static QComboBox *kindOf(MainWindow &window,const QString &field) { return window.findChild<QComboBox *>(field+"Kind"); }
    static QLineEdit *rangeOf(MainWindow &window,const QString &field) { return window.findChild<QLineEdit *>(field+"Range"); }
    static QStringList itemsOf(QComboBox *combo) { QStringList out;for(int i=0;i<combo->count();++i)out<<combo->itemText(i);return out; }
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
    // Makes the 0-based rectangle the grid's one selection, current cell at its top-left.
    static void selectCells(QTableView *grid,SheetModel *model,int top,int left,int bottom,int right) {
        grid->setCurrentIndex(model->index(top,left));
        grid->selectionModel()->select(QItemSelection(model->index(top,left),model->index(bottom,right)),QItemSelectionModel::ClearAndSelect);
    }
    static QString issuesText(MainWindow &window) { return window.findChild<QPlainTextEdit *>("operationIssues")->toPlainText(); }
    static QString statusText(MainWindow &window) {
        for(auto *label:window.findChildren<QLabel *>())if(label->accessibleName()=="Storage and calculation status")return label->text();
        return {};
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
    // Presses at a point of the widget and moves the pointer by delta in four
    // steps, releasing there when asked. Points are kept in global terms, as
    // the widget moves under the pointer. Returns the global end point.
    static QPoint dragBy(QWidget *widget,const QPoint &from,const QPoint &delta,bool release=true) {
        const QPoint start=widget->mapToGlobal(from);
        QTest::mousePress(widget,Qt::LeftButton,Qt::NoModifier,from);
        for(int step=1;step<=4;++step)QTest::mouseMove(widget,widget->mapFromGlobal(start+delta*step/4));
        if(release)QTest::mouseRelease(widget,Qt::LeftButton,Qt::NoModifier,widget->mapFromGlobal(start+delta));
        return start+delta;
    }
    // The 1-based cell of view under a point of the grid area and the
    // point's offset inside it, worked out from the view alone.
    static bool cellUnder(QTableView *view,QWidget *area,const QPoint &point,QJsonObject *anchor,QJsonObject *offset) {
        const QPoint local=view->viewport()->mapFrom(area,point);
        if(!view->viewport()->rect().contains(local))return false;
        const int row=view->rowAt(local.y()),col=view->columnAt(local.x());
        if(row<0 || col<0)return false;
        *anchor={{"row",row+1},{"col",col+1}};
        *offset={{"x",local.x()-view->columnViewportPosition(col)},{"y",local.y()-view->rowViewportPosition(row)}};
        return true;
    }
    // Waits until the window takes edits again (no operation in flight).
    static bool idle(MainWindow &window) {
        auto *insert=window.findChild<QAction *>("insertChartAction");
        for(int i=0;i<1000 && !insert->isEnabled();++i)QTest::qWait(10);
        return insert->isEnabled();
    }
    static int z(ChartLayer *layer,const QString &chartId) { return layer->chart(chartId)["z"].toInt(); }
    static ChartPanel *panelOf(MainWindow &window) { return window.findChild<ChartPanel *>("chartPanel"); }
    // The panel's "No chart selected" text.
    static QLabel *noChartLabel(ChartPanel *panel) {
        for(auto *label:panel->findChildren<QLabel *>())if(label->text()=="No chart selected")return label;
        return nullptr;
    }
    // Makes the window active, so focus changes send focus events.
    static void activate(MainWindow &window) { window.activateWindow();QVERIFY(QTest::qWaitForWindowActive(&window)); }
    // Selects the chart and opens the panel on it with Edit Chart.
    static bool openPanel(MainWindow &window,ChartLayer *layer,const QString &chartId) {
        layer->select(chartId);if(layer->selectedChart()!=chartId)return false;
        window.findChild<QAction *>("editChartAction")->trigger();
        return window.findChild<QDockWidget *>("chartDock")->isVisible() && panelOf(window)->chartId()==chartId;
    }
    // Replaces a field's text by typing, then presses Enter when asked.
    static void typeInto(QLineEdit *edit,const QString &text,bool enter) {
        edit->setFocus();edit->selectAll();
        if(text.isEmpty())QTest::keyClick(edit,Qt::Key_Backspace);else QTest::keyClicks(edit,text);
        if(enter)QTest::keyClick(edit,Qt::Key_Return);
    }
    // Chooses a list entry from the keyboard, as a person would.
    static void choose(QComboBox *combo,const QString &text) {
        // Typed letters search the list; a pause first starts a new search.
        QTest::qWait(QApplication::keyboardInputInterval()+50);combo->setFocus();QTest::keyClicks(combo,text);
        QCOMPARE(combo->currentText(),text);
    }
    // True when the overlay's own painting is the highlight colour at point.
    static bool highlighted(QWidget *widget,const QPoint &point) {
        return widget->grab().toImage().pixelColor(point*widget->devicePixelRatioF()).rgb()==widget->palette().color(QPalette::Highlight).rgb();
    }
};

QTEST_MAIN(ChartAuthoringTests)
#include "test_chart_authoring.moc"
