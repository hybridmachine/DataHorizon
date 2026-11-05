// SPDX-License-Identifier: GPL-3.0-only
// Explicit measurement executable: timings are evidence, not CI pass thresholds.
#include "mainwindow.h"
#include "frozen_row_probe.h"
#include "peak_rss.h"
#include <QApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLineEdit>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QPlainTextEdit>
#include <QSignalSpy>
#include <QSysInfo>
#include <QTableView>
#include <QTemporaryDir>
#include <QTest>
#include <QTimer>
#include <algorithm>
#include <cmath>
#include <stdexcept>

static void require(bool ok, const char *message) {
    if (!ok) throw std::runtime_error(message);
}
template<class Predicate> static void waitFor(Predicate ready, const char *phase) {
    QElapsedTimer deadline; deadline.start();
    while (!ready() && deadline.elapsed()<15000) QTest::qWait(1);
    require(ready(),phase);
}
static QJsonObject distribution(QList<double> values) {
    require(!values.isEmpty(),"No timing samples");
    std::sort(values.begin(),values.end());
    const int n=int(values.size());
    return {{"samples",n},{"median_ms",n%2?values[n/2]:(values[n/2-1]+values[n/2])/2},
            {"p95_ms",values[int(std::ceil(n*.95))-1]},{"max_ms",values.last()}};
}

int main(int argc, char **argv) {
    QApplication app(argc,argv);
    try {
        require(argc==2,"Usage: desktop_benchmark output.json");
        QTemporaryDir temp; require(temp.isValid(),"Cannot create benchmark directory");
        StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options);
        QSignalSpy service(&window,&MainWindow::serviceReady), opened(&window,&MainWindow::workbookReady);
        window.show(); waitFor([&]{return service.count()==1;},"Service startup timed out");
        if(QApplication::platformName()!="offscreen") {
            window.raise(); window.activateWindow();
            QCoreApplication::processEvents();
        }
        QElapsedTimer first; first.start();
        window.openBook(temp.filePath("benchmark.dhwb"),true);
        waitFor([&]{return opened.count()==1;},"Workbook open timed out");
        auto *grid=window.findChild<QTableView *>("grid");
        auto *address=window.findChild<QLineEdit *>("address");
        auto *model=window.model();
        auto visibleReady=[&] {
            const int top=qMax(0,grid->rowAt(0)), left=qMax(0,grid->columnAt(0));
            const int bottom=grid->rowAt(grid->viewport()->height()-1), right=grid->columnAt(grid->viewport()->width()-1);
            bool ready=bottom>=top && right>=left;
            for (int row=top;row<=bottom;++row) for (int col=left;col<=right;++col) {
                const auto index=model->index(row,col); model->data(index,Qt::DisplayRole);
                ready=ready && model->loaded(index);
            }
            return ready;
        };
        waitFor(visibleReady,"First viewport timed out"); grid->viewport()->repaint();
        const double firstMs=first.nsecsElapsed()/1e6;
        QJsonArray cells;
        for (int row=1;row<=10000;++row) cells.append(QJsonObject{{"row",row},{"col",1},{"source",QString::number(row)}});
        window.submit({{"action","edit"},{"cells",cells}});
        waitFor([&]{return opened.count()==2;},"Dataset edit timed out"); waitFor(visibleReady,"Dataset viewport timed out");
        int requests=0;
        auto *network=window.findChild<QNetworkAccessManager *>(); require(network,"Missing REST transport");
        QObject::connect(network,&QNetworkAccessManager::finished,&app,[&](QNetworkReply *reply) {
            if (reply->url().path().endsWith("/range")) ++requests;
        });
        QElapsedTimer clock; clock.start();
        qint64 previous=clock.nsecsElapsed();
        QList<double> gaps, scroll, jumps, input;
        QTimer heartbeat; heartbeat.setTimerType(Qt::PreciseTimer); heartbeat.setInterval(1);
        QObject::connect(&heartbeat,&QTimer::timeout,&app,[&] {
            const auto now=clock.nsecsElapsed(); gaps.append((now-previous)/1e6); previous=now;
        });
        heartbeat.start();
        auto navigate=[&](int row, QList<double> &samples) {
            QElapsedTimer elapsed; elapsed.start();
            grid->scrollTo(model->index(row,0),QAbstractItemView::PositionAtCenter);
            // Dispatch input while uncached viewport requests are in flight.
            QCoreApplication::processEvents();
            QElapsedTimer keys; keys.start();
            address->setFocus(); address->selectAll(); QTest::keyClicks(address,"A12345");
            require(address->text()=="A12345","Address input was lost during scrolling");
            input.append(keys.nsecsElapsed()/1e6);
            try { waitFor(visibleReady,"Scroll viewport timed out"); }
            catch (...) { qCritical()<<"Row"<<row<<"issues:"<<window.findChild<QPlainTextEdit *>()->toPlainText(); throw; }
            grid->viewport()->repaint();
            samples.append(elapsed.nsecsElapsed()/1e6);
        };
        for (int sample=0;sample<100;++sample) navigate(40+sample*64,scroll);
        const int scrollRequests=requests;
        for (int sample=0;sample<100;++sample) navigate(sample%2?900000+sample*64:10000+sample*64,jumps);
        const int jumpRequests=requests-scrollRequests;
        heartbeat.stop();
        QJsonArray headers;
        for(int col=1;col<=256;++col) headers.append(QJsonObject{{"row",1},{"col",col},{"source",QString("Header %1").arg(col)},{"kind","text"}});
        window.submit({{"action","edit"},{"cells",headers}});
        waitFor([&]{return opened.count()==3;},"Header fixture edit timed out");
        FrozenRowProbe frozen(grid);
        grid->setColumnWidth(0,150);
        grid->setRowHeight(0,30);
        int edits=0;
        QObject::connect(model,&SheetModel::editRequested,&frozen.view,[&]{++edits;});
        auto frozenReady=[&] {
            const int left=qMax(0,frozen.view.columnAt(0));
            const int right=frozen.view.columnAt(frozen.view.viewport()->width()-1);
            bool ready=right>=left;
            for(int col=left;col<=right;++col) {
                frozen.model.data(frozen.model.index(0,col));
                ready=ready && model->loaded(model->index(0,col));
            }
            return ready;
        };
        QList<double> frozenTimes, frozenInput;
        bool focusedRetention=true;
        const int frozenStart=requests;
        previous=clock.nsecsElapsed(); heartbeat.start();
        for(int sample=0;sample<100;++sample) {
            if(sample==50) { window.resize(820,620); QCoreApplication::processEvents(); }
            QElapsedTimer elapsed; elapsed.start();
            grid->scrollTo(model->index(20000+sample*64,(sample%4)*32),QAbstractItemView::PositionAtTop);
            QCoreApplication::processEvents();
            QElapsedTimer keys; keys.start();
            address->setFocus(); address->selectAll(); QTest::keyClicks(address,"B42");
            require(address->text()=="B42","Input was lost during frozen-row scrolling");
            const bool focusedBefore=address->hasFocus();
            frozenInput.append(keys.nsecsElapsed()/1e6);
            waitFor([&]{return visibleReady() && frozenReady();},"Frozen/body viewport timed out");
            const bool retainedFocus=focusedBefore && address->hasFocus();
            focusedRetention=focusedRetention && retainedFocus;
            if(retainedFocus) require(address->text()=="B42","Viewport reply overwrote the address draft");
            const int col=qMax(0,frozen.view.columnAt(0));
            const QRect top=frozen.view.visualRect(frozen.model.index(0,col));
            const QRect body=grid->visualRect(model->index(grid->rowAt(0),col));
            require(frozen.view.pos()==grid->viewport()->pos() && frozen.view.isVisible(),"Frozen overlay moved out of its viewport");
            require(top.left()==body.left() && top.width()==body.width() && top.top()==0,"Frozen column geometry drifted");
            require(frozen.model.data(frozen.model.index(0,col)).toString()==QString("Header %1").arg(col+1),"Frozen header changed with vertical scrolling");
            frozen.view.viewport()->repaint();
            frozenTimes.append(elapsed.nsecsElapsed()/1e6);
        }
        require(edits==0,"Frozen navigation generated a managed edit");
        if(QApplication::platformName()=="offscreen") require(focusedRetention,"Offscreen input probe lacked address focus");
        const auto cache=model->cacheMetrics();
        require(cache["tiles"].toInt()<=16 && cache["tile_charge_bytes"].toInt()<=cache["tile_budget_bytes"].toInt(),"Frozen view exceeded shared tile cache");
        require(cache["source_charge_bytes"].toInt()<=cache["source_budget_bytes"].toInt(),"Source cache exceeded budget");
        const QFileInfo reportPath(QString::fromLocal8Bit(argv[1]));
        const auto screenshot=reportPath.dir().filePath(reportPath.completeBaseName()+"-frozen-row.png");
        require(window.grab().save(screenshot),"Cannot save frozen-row screenshot");
        QTest::qWait(5); heartbeat.stop();
        QJsonObject report{{"recorded_at_utc",QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
            {"os",QSysInfo::prettyProductName()},{"architecture",QSysInfo::currentCpuArchitecture()},
            {"qt",qVersion()},{"platform_plugin",QApplication::platformName()},
            {"gui_peak_rss_bytes",double(processPeakRssBytes())},
            {"dataset","10,000 numeric cells in column A; distant jumps include empty sparse regions"},
            {"first_blank_usable_viewport_ms",firstMs},{"scroll_to_usable_viewport",distribution(scroll)},
            {"distant_jump_to_usable_viewport",distribution(jumps)},{"synthetic_address_key_dispatch",distribution(input)},
            {"event_loop_gap",distribution(gaps)},{"scroll_range_responses",scrollRequests},
            {"distant_jump_range_responses",jumpRequests},
            {"frozen_row",QJsonObject{{"scroll_to_usable_viewport",distribution(frozenTimes)},
                {"synthetic_address_key_dispatch",distribution(frozenInput)},
                {"focused_address_retention_checked_all_samples",focusedRetention},
                {"range_responses",requests-frozenStart},{"managed_edit_requests",edits},{"cache",cache},
                {"screenshot",QFileInfo(screenshot).fileName()},{"scope","Read-only display probe; not installed or persisted"}}},
            {"targets",QJsonObject{{"viewport_p95_ms",150},{"input_stall_ms",50}}},
            {"limitations",QJsonArray{"One process launch; first viewport is not a cold OS-cache measurement.",
                "Synthetic Qt input and synchronous Qt paints exclude physical input/compositor latency.",
                "Offscreen runs do not qualify native interaction, accessibility, high DPI or another platform.",
                "Focused address retention is only checked when widget focus is available; inspect its explicit flag in native runs.",
                "RSS applies to this 10,000-cell GUI workload; frozen-row editing/focus/selection, persistence, hidden columns and multiple frozen edges remain unqualified."}}};
        QFile output(QString::fromLocal8Bit(argv[1])); require(output.open(QIODevice::WriteOnly),"Cannot write benchmark report");
        const auto bytes=QJsonDocument(report).toJson(QJsonDocument::Indented);
        require(output.write(bytes)==bytes.size(),"Cannot finish benchmark report");
        return 0;
    } catch (const std::exception &error) {
        qCritical("%s",error.what()); return 1;
    }
}
