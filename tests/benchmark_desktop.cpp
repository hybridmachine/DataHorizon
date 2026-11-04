// SPDX-License-Identifier: GPL-3.0-only
// Explicit measurement executable: timings are evidence, not CI pass thresholds.
#include "mainwindow.h"
#include <QApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QFile>
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
        QTest::qWait(5); heartbeat.stop();
        QJsonObject report{{"recorded_at_utc",QDateTime::currentDateTimeUtc().toString(Qt::ISODate)},
            {"os",QSysInfo::prettyProductName()},{"architecture",QSysInfo::currentCpuArchitecture()},
            {"qt",qVersion()},{"platform_plugin",QApplication::platformName()},
            {"dataset","10,000 numeric cells in column A; distant jumps include empty sparse regions"},
            {"first_blank_usable_viewport_ms",firstMs},{"scroll_to_usable_viewport",distribution(scroll)},
            {"distant_jump_to_usable_viewport",distribution(jumps)},{"synthetic_address_key_dispatch",distribution(input)},
            {"event_loop_gap",distribution(gaps)},{"scroll_range_responses",scrollRequests},
            {"distant_jump_range_responses",requests-scrollRequests},
            {"targets",QJsonObject{{"viewport_p95_ms",150},{"input_stall_ms",50}}},
            {"limitations",QJsonArray{"One process launch; first viewport is not a cold OS-cache measurement.",
                "Synthetic Qt input and synchronous Qt paints exclude physical input/compositor latency.",
                "Offscreen runs do not qualify native interaction, accessibility, high DPI or another platform.",
                "RSS and frozen panes are not measured by this executable."}}};
        QFile output(QString::fromLocal8Bit(argv[1])); require(output.open(QIODevice::WriteOnly),"Cannot write benchmark report");
        const auto bytes=QJsonDocument(report).toJson(QJsonDocument::Indented);
        require(output.write(bytes)==bytes.size(),"Cannot finish benchmark report");
        return 0;
    } catch (const std::exception &error) {
        qCritical("%s",error.what()); return 1;
    }
}
