// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "frozen_row_probe.h"
#include <QAbstractItemModelTester>
#include <QApplication>
#include <QAction>
#include <QJsonArray>
#include <QLineEdit>
#include <QItemSelectionModel>
#include <QRegularExpression>
#include <QLabel>
#include <QSignalSpy>
#include <QStandardItemModel>
#include <QScopeGuard>
#include <QTableView>
#include <QPlainTextEdit>
#include <QPointer>
#include <QPainter>
#include <QInputDialog>
#include <QSettings>
#include <QAbstractButton>
#include <QPushButton>
#include <QAbstractItemDelegate>
#include <QClipboard>
#include <QFile>
#include <QFileInfo>
#include <QFileDialog>
#include <QDir>
#include <QDialogButtonBox>
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QMessageBox>
#include <QMimeData>
#include <QTcpServer>
#include <QTcpSocket>
#include <QNetworkReply>
#include <QNetworkProxy>
#include <QTabBar>
#include <QTimer>
#include <QStyleOptionViewItem>
#include <QTemporaryDir>
#include <QTest>

class DelayedClipboardServer : public QTcpServer {
public:
    struct Pending { QPointer<QTcpSocket> socket; QJsonObject range; };
    QList<Pending> requests;
    DelayedClipboardServer(const QString &state, const QString &sheet) : m_state(state), m_sheet(sheet) {
        connect(this,&QTcpServer::newConnection,this,[this] {
            auto *socket=nextPendingConnection();
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket] {
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                const auto end=request.indexOf("\r\n\r\n"); if(end<0 || socket->property("handled").toBool()) return;
                const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption)
                    .match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                if(request.size()<end+4+length) return;
                socket->setProperty("handled",true);
                if(request.startsWith("POST ") && request.contains("/clipboard HTTP/"))
                    requests.append({socket,QJsonDocument::fromJson(request.mid(end+4)).object()});
                else send(socket,{{"state",m_state},{"sheet",m_sheet},{"cells",QJsonArray{}}});
            });
        });
    }
    void respond(int index, const QString &source, bool rejected = false) {
        const auto &pending=requests.at(index);
        if(rejected) send(pending.socket,{{"code","READ_FAILED"},{"message","Superseded clipboard read failed"}},true);
        else send(pending.socket,{{"state",m_state},{"sheet",m_sheet},{"cells",QJsonArray{QJsonObject{
            {"row",pending.range["r1"]},{"col",pending.range["c1"]},{"source",source},{"display",source},
            {"value",source},{"input_kind","text"},{"kind","text"},{"style",QJsonObject{}}}}}});
    }
private:
    static void send(QTcpSocket *socket, const QJsonObject &result, bool rejected = false) {
        const auto body=QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write(QByteArray("HTTP/1.1 ")+(rejected?"409 Conflict":"200 OK")
            +"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
        socket->disconnectFromHost();
    }
    QString m_state, m_sheet;
};

class DelayedCloseProxy : public QTcpServer {
public:
    struct Pending { QPointer<QTcpSocket> socket; QJsonObject result; QString error; };
    Pending closeReply, metadataReply, rangeReply, openReply;
    bool holdMetadata = false, holdRange = false, holdOpen = false;
    QString closeFailure, closedHandle;
    int mutations = 0, opens = 0, closes = 0, metadataReads = 0;
    DelayedCloseProxy(const QJsonObject &service) : m_upstream(this) {
        m_upstream.configure(service["url"].toString(),service["token"].toString());
        connect(this,&QTcpServer::newConnection,this,[this] {
            auto *socket=nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket] {
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                const auto end=request.indexOf("\r\n\r\n"); if(end<0 || socket->property("handled").toBool()) return;
                const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption)
                    .match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                if(request.size()<end+4+length) return;
                socket->setProperty("handled",true);
                const auto path=QString::fromLatin1(request.left(request.indexOf("\r\n")).split(' ').at(1));
                const auto parts=path.split('/',Qt::SkipEmptyParts);
                const bool post=request.startsWith("POST "), metadata=!post && parts.size()==3 && parts[1]=="workbooks";
                if(path=="/v1/open") ++opens;
                if(path.endsWith("/operations")) ++mutations;
                if(metadata) ++metadataReads;
                if(path.endsWith("/close")) {
                    ++closes; closedHandle=parts[2];
                    if(!closeFailure.isEmpty()) {
                        Pending failed{socket,{},"Close service unavailable"};
                        if(m_closeFailureReleased) failClose(failed); else closeReply=failed;
                        return;
                    }
                }
                const QPointer<QTcpSocket> guarded=socket;
                m_upstream.request(path,QJsonDocument::fromJson(request.mid(end+4)).object(),
                    [this,guarded,path,metadata](QJsonObject result,QString error) {
                        if(!guarded) return;
                        Pending response{guarded,result,error};
                        if(path.endsWith("/close") && error.isEmpty() && result["closed"].toBool()) closeReply=response;
                        else if(path=="/v1/open" && holdOpen) openReply=response;
                        else if(metadata && holdMetadata) metadataReply=response;
                        else if(path.endsWith("/range") && holdRange) rangeReply=response;
                        else send(response);
                    },post);
            });
        });
    }
    void releaseClose() {
        if(!closeFailure.isEmpty()) { m_closeFailureReleased=true; failClose(closeReply); }
        else send(closeReply);
        closeReply={};
    }
    void releaseMetadata() { holdMetadata=false; send(metadataReply); }
    void releaseRange() { holdRange=false; send(rangeReply); }
    void releaseOpen() { holdOpen=false; send(openReply); }
private:
    void failClose(const Pending &response) {
        if(closeFailure=="disconnect") response.socket->abort(); else send(response);
    }
    static void send(const Pending &response) {
        const auto body=QJsonDocument(response.error.isEmpty()?response.result:QJsonObject{{"code","UNAVAILABLE"},{"message",response.error}})
            .toJson(QJsonDocument::Compact);
        response.socket->write(QByteArray("HTTP/1.1 ")+(response.error.isEmpty()?"200 OK":"503 Service Unavailable")
            +"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
        response.socket->disconnectFromHost();
    }
    RestClient m_upstream;
    bool m_closeFailureReleased = false;
};

class DesktopTests : public QObject {
    Q_OBJECT
private slots:
    void frozenRowProbeKeepsOneSourceRowAcrossChangesAndReset() {
        QStandardItemModel source(3,8);
        source.setData(source.index(0,0),"Header");
        FirstRowModel first(&source);
        QAbstractItemModelTester tester(&first,QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(first.rowCount(),1); QCOMPARE(first.columnCount(),8);
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("Header"));
        QVERIFY(!(first.flags(first.index(0,0)) & Qt::ItemIsEditable));
        QSignalSpy changed(&first,&QAbstractItemModel::dataChanged);
        source.setData(source.index(1,0),"Body"); QCOMPARE(changed.count(),0);
        source.setData(source.index(0,0),"Updated"); QCOMPARE(changed.count(),1);
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("Updated"));
        source.insertRow(0);
        source.setData(source.index(0,0),"New header");
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("New header"));
        source.removeRow(0);
        QCOMPARE(first.data(first.index(0,0)).toString(),QString("Updated"));
        source.clear(); QCOMPARE(first.rowCount(),0); QCOMPARE(first.columnCount(),0);
    }
    void frozenRowProbeRemainsVisibleDuringViewportScrolling() {
        QStandardItemModel source(200,20);
        source.setData(source.index(0,0),"Header");
        QTableView grid; grid.setModel(&source); grid.resize(500,300); grid.show();
        FrozenRowProbe frozen(&grid);
        QCoreApplication::processEvents();
        grid.scrollTo(source.index(100,10),QAbstractItemView::PositionAtTop);
        QCoreApplication::processEvents();
        QCOMPARE(frozen.view.pos(),grid.viewport()->pos());
        QVERIFY(frozen.view.isVisible());
        QCOMPARE(frozen.view.width(),grid.viewport()->width());
    }
    void viewportRepliesPreserveAnAddressBeingTyped() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("address-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        auto *address=window.findChild<QLineEdit *>("address");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),3000);
        QSignalSpy loaded(model,&SheetModel::tileLoaded);
        model->data(model->index(64000,0));
        address->setFocus(); address->selectAll(); QTest::keyClicks(address,"B42");
        QTRY_VERIFY_WITH_TIMEOUT(loaded.count()>0,3000);
        QCOMPARE(address->text(),QString("B42"));
        QTest::keyClick(address,Qt::Key_Return);
        QTRY_COMPARE(grid->currentIndex(),model->index(41,1));
        QCOMPARE(address->text(),QString("B42"));
    }
    void scrollingPastTileLimitKeepsTheVisibleViewportLoaded() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("scroll.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        auto visibleReady=[&] {
            bool loaded=true;
            for(int row=grid->rowAt(0);row<=grid->rowAt(grid->viewport()->height()-1);++row)
                for(int col=grid->columnAt(0);col<=grid->columnAt(grid->viewport()->width()-1);++col) {
                    const auto index=model->index(row,col); model->data(index);
                    loaded=loaded && model->loaded(index);
                }
            return loaded;
        };
        for (int sample=0;sample<40;++sample) {
            grid->scrollTo(model->index(40+sample*64,0),QAbstractItemView::PositionAtCenter);
            QCoreApplication::processEvents();
            QTRY_VERIFY_WITH_TIMEOUT(visibleReady(),3000);
            const auto cache=model->cacheMetrics();
            QVERIFY(cache["tiles"].toInt()<=16);
            QVERIFY(cache["tile_charge_bytes"].toInt()<=cache["tile_budget_bytes"].toInt());
        }
    }
    void wholeRowColumnCommandsAdjustReferencesAndWarnOnDeletion() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("structure.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","7"}},
            QJsonObject{{"row",4},{"col",2},{"source","=$A$2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        const auto original=model->state(); grid->selectRow(1);
        answerNextDialog(QMessageBox::Cancel); window.findChild<QAction *>("insertRowsAction")->trigger();
        QCOMPARE(model->state(),original);
        answerNextDialog(QMessageBox::Yes); window.findChild<QAction *>("insertRowsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(4,1),Qt::EditRole).toString(),QString("=$A$3"),5000);
        QCOMPARE(model->data(model->index(4,1)).toString(),QString("7"));
        grid->selectColumn(0); answerNextDialog(QMessageBox::Yes);
        window.findChild<QAction *>("insertColumnsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(4,2),Qt::EditRole).toString(),QString("=$B$3"),5000);
        grid->selectRow(2); answerNextDialog(QMessageBox::Yes);
        window.findChild<QAction *>("deleteRowsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,2)).toString(),QString("#REF!"),5000);
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("1 reference expressions became #REF!"));
        grid->selectColumn(1); answerNextDialog(QMessageBox::Yes);
        window.findChild<QAction *>("deleteColumnsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,1),Qt::EditRole).toString(),QString("=#REF!"),5000);
        model->setProjection({0,2});
        window.findChild<QAction *>("insertRowsAction")->trigger();
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("Clear the list view before changing rows or columns"));
        QCOMPARE(opened.count(),6);
    }
    void rowDeletionConfirmationRetainsReviewedStateAfterExternalRefresh() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("structure-race.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
        QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
        QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
        connect(&writer,&MainWindow::workbookReady,&window,[&]{window.model()->externalChange();});
        bool confirmed=false;
        connect(&window,&MainWindow::workbookReady,&window,[&]{
            if (auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                confirmed=true; box->button(QMessageBox::Yes)->click();
            }
        });
        window.findChild<QTableView *>("grid")->selectRow(1);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QTimer::singleShot(0,&writer,[&]{writer.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","9"}}}}});});
        QTimer::singleShot(10000,&window,[]{
            if (auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) box->button(QMessageBox::Cancel)->click();
        });
        window.findChild<QAction *>("deleteRowsAction")->trigger();
        QVERIFY(confirmed); QCOMPARE(changed.count(),0);
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("Reviewed inputs changed"));
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(1,0)).toString(),QString("9"),5000);
    }
    void listSaveRetainsItsSourceCellAfterResorting() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("list-save.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","10"}},QJsonObject{{"row",2},{"col",1},{"source","20"}},
            QJsonObject{{"row",3},{"col",1},{"source","30"}},QJsonObject{{"row",2},{"col",2},{"source","original"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source"); auto *model=window.model();
        grid->setCurrentIndex(model->index(1,1)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"updated");
        window.findChild<QAction *>("saveAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(model->sourceRow(grid->currentIndex().row()),1); QCOMPARE(grid->currentIndex().column(),1);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(grid->currentIndex()).toString(),QString("updated"),5000);
        grid->setCurrentIndex(model->index(1,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"5");
        window.findChild<QAction *>("saveAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QCOMPARE(model->sourceRow(grid->currentIndex().row()),1); QCOMPARE(grid->currentIndex(),model->index(2,0));
        QTRY_COMPARE_WITH_TIMEOUT(model->data(grid->currentIndex()).toString(),QString("5"),5000);
    }
    void modelInvariants() {
        RestClient client;
        SheetModel model(&client, nullptr, 64, 32);
        QAbstractItemModelTester tester(&model, QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(model.headerData(26, Qt::Horizontal, Qt::DisplayRole).toString(), QString("AA"));
    }
    void managedSupplies() {
        QTemporaryDir temp;
        StartupOptions options; options.automaticWorkbook = false;
        MainWindow window(options); window.show();
        QSignalSpy service(&window, &MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(service.count(), 1, 10000);
        QSignalSpy opened(&window, &MainWindow::workbookReady);
        window.openBook(temp.filePath("supplies.dhwb"), true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(), 1, 10000);
        auto *model = window.model();
        auto *grid = window.findChild<QTableView *>("grid");
        auto *source = window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)), 5000);
        QSignalSpy changed(&window, &MainWindow::operationFinished);
        QJsonArray cells;
        auto cell = [&cells](int row, int col, const QString &text) { cells.append(QJsonObject{{"row", row}, {"col", col}, {"source", text}}); };
        cell(1,1,"Item"); cell(1,2,"Quantity"); cell(1,3,"Unit price"); cell(1,4,"Amount");
        cell(2,1,"Notebook"); cell(2,2,"2"); cell(2,3,"3.5"); cell(2,4,"=B2*C2");
        cell(3,1,"Pen"); cell(3,2,"4"); cell(3,3,"1.25"); cell(3,4,"=B3*C3");
        cell(4,1,"Folder"); cell(4,2,"3"); cell(4,3,"2"); cell(4,4,"=B4*C4");
        cell(6,3,"Total"); cell(6,4,"=SUM(D2:D4)");
        window.submit({{"action","edit"},{"cells",cells}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(), 1, 10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 2, 10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(), QString("18"), 5000);
        grid->setCurrentIndex(model->index(2,1)); source->setFocus(); source->selectAll();
        QTest::keyClicks(source,"6"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),2,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(), QString("20.5"),5000);
        window.submit({{"action","undo"}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("18"),5000);
        // A projected edit must use the source row, even after reordering.
        model->setProjection({0, 3, 2});
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("Folder"),5000);
        QCOMPARE(model->headerData(1,Qt::Vertical,Qt::DisplayRole).toInt(),4);
        grid->setCurrentIndex(model->index(1,1));
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"5"); QTest::keyClick(source,Qt::Key_Escape);
        QCOMPARE(source->text(),QString("3"));
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"4"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),4,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(3,1)).toString(),QString("4"),5000);
        window.submit({{"action","undo"}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),5,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 6,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("18"),5000);
        grid->setCurrentIndex(model->index(2,1)); source->setFocus(); source->selectAll(); QTest::keyClicks(source,"6");
        window.findChild<QAction *>("saveAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),6,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 7,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("20.5"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.findChild<QLabel *>("storageStatus")->text(),QString("Stored automatically • Save confirmed"),5000);
        QJsonArray formats;
        for(int row=2;row<=6;++row) formats.append(QJsonObject{{"row",row},{"col",4},{"style",QJsonObject{{"format","fixed2"}}}});
        window.submit({{"action","edit"},{"cells",formats}});
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),7,10000);
        QTRY_VERIFY_WITH_TIMEOUT(opened.count() >= 8,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(5,3)).toString(),QString("20.50"),5000);
        auto *address = window.findChild<QLineEdit *>("address");
        address->setText("XFD1048576"); QTest::keyClick(address,Qt::Key_Return);
        QCOMPARE(grid->currentIndex(),model->index(1048575,16383));
        address->setText("A1"); QTest::keyClick(address,Qt::Key_Return);
        auto screenshot = qEnvironmentVariable("DATAHORIZON_SCREENSHOT");
        if (!screenshot.isEmpty()) { QTest::qWait(300); QVERIFY(window.grab().save(screenshot)); }
    }
    void managedRequiredHook() {
        QTemporaryDir temp;
        StartupOptions options; options.automaticWorkbook=false; options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.openBook(temp.filePath("hooks.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QVERIFY(model->setData(model->index(0,0),"8",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000);
        QVERIFY(changed[0][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("16"),5000);
        QVERIFY(model->setData(model->index(0,0),"-1",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),2,10000);
        QVERIFY(!changed[1][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(opened.count()>=3,10000);
        auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("-1"),5000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QVERIFY(source->isModified());
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("8"));
        QCOMPARE(model->data(model->index(0,1)).toString(),QString("16"));
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("nonnegative"));
    }
    void closingWindowRequiresExplicitDiscardOfAnActiveCellDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("close-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        grid->edit(window.model()->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"unsubmitted input");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::No);
        QVERIFY(!window.close());
        QCOMPARE(source->text(),QString("unsubmitted input")); QVERIFY(source->isModified());
        QCOMPARE(changed.count(),0); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        answerNextDialog(QMessageBox::Yes); QVERIFY(window.close());
    }
    void openingWorkbookRequiresExplicitDiscardOfAnActiveCellDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("first.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        const auto original=window.model()->workbook();
        grid->edit(window.model()->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"unsubmitted input");
        const auto second=temp.filePath("second.dhwb");
        answerNextDialog(QMessageBox::No); window.openBook(second,true);
        QCOMPARE(window.model()->workbook(),original); QCOMPARE(opened.count(),1); QVERIFY(!QFile::exists(second));
        QCOMPARE(source->text(),QString("unsubmitted input")); QVERIFY(source->isModified());
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        answerNextDialog(QMessageBox::Yes); window.openBook(second,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QVERIFY(window.model()->workbook()!=original);
    }
    void attachedHandleReleaseKeepsInteractionLocked_data() {
        QTest::addColumn<QString>("pendingRead");
        QTest::newRow("no-pending-read") << QString();
        QTest::newRow("newer-metadata") << QString("metadata");
        QTest::newRow("pending-range") << QString("range");
    }
    void attachedHandleReleaseKeepsInteractionLocked() {
        QFETCH(QString,pendingRead);
        QTemporaryDir temp; QJsonObject service; StartupOptions ownerOptions; ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions); observeOwnedServiceStartup(owner,service); QSignalSpy serviceReady(&owner,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(serviceReady.count(),1,10000); QVERIFY(!service["token"].toString().isEmpty());
        const auto path=temp.filePath("attached-release.dhwb");
        QSignalSpy ownerOpened(&owner,&MainWindow::workbookReady); owner.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),1,10000);
        owner.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),2,10000);
        DelayedCloseProxy proxy(service); QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        StartupOptions options; options.automaticWorkbook=false; options.url="http://127.0.0.1:"+QString::number(proxy.serverPort());
        options.token=service["token"].toString(); options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *source=window.findChild<QLineEdit *>("source"); auto *tabs=window.findChild<QTabBar *>("sheets");
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        const auto handle=model->workbook(), sheet=model->sheet(), state=model->state();
        if(pendingRead=="metadata") {
            owner.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","independent change"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),3,10000);
            proxy.holdMetadata=true;
            QAction *refresh=nullptr; for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Refresh stored data") refresh=action;
            QVERIFY(refresh); refresh->trigger(); QTRY_VERIFY_WITH_TIMEOUT(proxy.metadataReply.socket,3000);
            QVERIFY(proxy.metadataReply.result["state"].toString()!=state);
        } else if(pendingRead=="range") {
            proxy.holdRange=true; model->data(model->index(64,0)); QTRY_VERIFY_WITH_TIMEOUT(proxy.rangeReply.socket,3000);
        }
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000);
        QVERIFY(proxy.closeReply.error.isEmpty()); QVERIFY(proxy.closeReply.result["closed"].toBool()); QCOMPARE(proxy.closedHandle,handle);
        if(pendingRead=="metadata") {
            proxy.releaseMetadata(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,3000); QVERIFY(model->state()!=state);
        } else if(pendingRead=="range") {
            proxy.releaseRange(); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(64,0)),3000);
        }
        QVERIFY(window.isVisible()); QVERIFY(!source->isEnabled()); QVERIFY(!tabs->isEnabled());
        QVERIFY(!(model->flags(model->index(0,0))&Qt::ItemIsEditable));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        source->returnPressed();
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","must not submit"}}}}});
        const auto second=temp.filePath("must-not-open.dhwb"); window.openBook(second,true); tabs->setCurrentIndex(1);
        QTest::qWait(50); QCOMPARE(proxy.mutations,0); QCOMPARE(changed.count(),0); QCOMPARE(proxy.opens,1);
        QCOMPARE(model->sheet(),sheet); QVERIFY(!QFile::exists(second));
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),3000); QCOMPARE(proxy.closes,1);
        QCOMPARE(owner.findChild<QProcess *>("ownedService")->state(),QProcess::Running);
        QVERIFY(QDir(options.recoveryDirectory).entryList({"operation-*.json"},QDir::Files).isEmpty());
    }
    void failedAttachedHandleCleanupStillCloses_data() {
        QTest::addColumn<QString>("failure");
        QTest::newRow("http-error") << QString("http");
        QTest::newRow("disconnected-service") << QString("disconnect");
    }
    void failedAttachedHandleCleanupStillCloses() {
        QFETCH(QString,failure);
        QTemporaryDir temp; QJsonObject service; StartupOptions ownerOptions; ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions); observeOwnedServiceStartup(owner,service); QSignalSpy serviceReady(&owner,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(serviceReady.count(),1,10000); QVERIFY(!service["token"].toString().isEmpty());
        const auto path=temp.filePath("failed-close.dhwb");
        QSignalSpy ownerOpened(&owner,&MainWindow::workbookReady); owner.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(ownerOpened.count(),1,10000);
        DelayedCloseProxy proxy(service); proxy.closeFailure=failure; QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        StartupOptions options; options.automaticWorkbook=false; options.url="http://127.0.0.1:"+QString::number(proxy.serverPort());
        options.token=service["token"].toString(); options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *source=window.findChild<QLineEdit *>("source"); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000); QVERIFY(!source->isEnabled());
        int completedCloses=0;
        connect(window.findChild<RestClient *>()->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply) {
            if(reply->url().path().endsWith("/close")) ++completedCloses;
        });
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","must not submit"}}}}});
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),3000);
        QCOMPARE(proxy.mutations,0); QCOMPARE(completedCloses,1); QVERIFY(proxy.closes>=1); QCOMPARE(proxy.metadataReads,0);
        if(failure=="disconnect") {
            const auto attempts=proxy.closes;
            QTcpSocket retry; retry.setProxy(QNetworkProxy::NoProxy); retry.connectToHost(QHostAddress::LocalHost,proxy.serverPort());
            QTRY_COMPARE_WITH_TIMEOUT(retry.state(),QAbstractSocket::ConnectedState,3000);
            retry.write("POST /v1/workbooks/retried/close HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}");
            QTRY_VERIFY_WITH_TIMEOUT(proxy.closes>attempts,3000);
            QTRY_COMPARE_WITH_TIMEOUT(retry.state(),QAbstractSocket::UnconnectedState,3000);
            QVERIFY(!proxy.closeReply.socket); QCOMPARE(completedCloses,1);
        }
        QCOMPARE(owner.findChild<QProcess *>("ownedService")->state(),QProcess::Running);
        QVERIFY(QDir(options.recoveryDirectory).entryList({"operation-*.json"},QDir::Files).isEmpty());
    }
    void releasedHandleCompletionCannotCloseANewerWorkbook() {
        QTemporaryDir temp; QJsonObject service; StartupOptions ownerOptions; ownerOptions.automaticWorkbook=false;
        MainWindow owner(ownerOptions); observeOwnedServiceStartup(owner,service); QSignalSpy serviceReady(&owner,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(serviceReady.count(),1,10000); QVERIFY(!service["token"].toString().isEmpty());
        DelayedCloseProxy proxy(service); QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        StartupOptions options; options.automaticWorkbook=false; options.url="http://127.0.0.1:"+QString::number(proxy.serverPort());
        options.token=service["token"].toString();
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("first-close-context.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *source=window.findChild<QLineEdit *>("source"); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        const auto first=window.model()->workbook(); proxy.holdOpen=true;
        window.openBook(temp.filePath("second-close-context.dhwb"),true); QTRY_VERIFY_WITH_TIMEOUT(proxy.openReply.socket,3000);
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000); QCOMPARE(proxy.closedHandle,first);
        proxy.releaseOpen(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,3000);
        const auto second=window.model()->workbook(); QVERIFY(second!=first); QVERIFY(!source->isEnabled());
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000); QVERIFY(window.isVisible());
        QCOMPARE(window.model()->workbook(),second);
        QVERIFY(!window.close()); QTRY_VERIFY_WITH_TIMEOUT(proxy.closeReply.socket,3000); QCOMPARE(proxy.closedHandle,second);
        proxy.releaseClose(); QTRY_VERIFY_WITH_TIMEOUT(!window.isVisible(),3000); QCOMPARE(proxy.mutations,0);
    }
    void enterAdvancesAfterCommitAndSaveKeepsTheCurrentCell() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("enter.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"7"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,0)),5000);
        grid->edit(model->index(1,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"8"); QTest::keyClick(editor,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(grid->currentIndex(),model->index(2,0)); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"9"); QTest::keyClick(source,Qt::Key_Return,Qt::ShiftModifier);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"10");
        window.findChild<QAction *>("saveAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("10"),5000);
        grid->setCurrentIndex(model->index(1048575,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"11"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);
        QCOMPARE(grid->currentIndex(),model->index(1048575,0));
        const QJsonArray sort{QJsonObject{{"col",1},{"descending",true}}};
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"sort",sort}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000); QCOMPARE(model->sourceRow(0),1);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"6"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),8,10000);
        QCOMPARE(model->sourceRow(grid->currentIndex().row()),2);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(grid->currentIndex()).toString(),QString("9"),5000);
    }
    void rejectedEnterKeepsItsCellAndDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("enter-rejected.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        source->setFocus(); QTest::keyClicks(source,"-1"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QCOMPARE(grid->currentIndex(),model->index(0,0)); QCOMPARE(source->text(),QString("-1")); QVERIFY(source->isModified());
        QTest::keyClick(source,Qt::Key_Escape);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"-2"); QTest::keyClick(editor,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(grid->currentIndex(),model->index(0,0)); QCOMPARE(source->text(),QString("-2")); QVERIFY(source->isModified());
    }
    void cancellingARangeActionPreservesTheActiveCellDraftBeforeMutation() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("range-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"retained input");
        QAction *bold=nullptr; for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Bold") bold=action;
        QVERIFY(bold); QSignalSpy changed(&window,&MainWindow::operationFinished); QString question;
        answerNextDialog(QMessageBox::Cancel,&question); bold->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(!question.isEmpty(),5000);
        QCOMPARE(changed.count(),0); QCOMPARE(opened.count(),1);
        QCOMPARE(source->text(),QString("retained input")); QVERIFY(source->isModified()); QVERIFY(source->isEnabled());
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        QTest::keyClick(source,Qt::Key_Escape); bold->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QVERIFY(model->cell(model->index(0,0))["style"].toObject()["bold"].toBool());
        QCOMPARE(model->cell(model->index(0,0))["source"].toString(),QString());
    }
    void ownedServiceIgnoresAmbientPythonModules() {
        QTemporaryDir temp;
        const auto previous=QDir::currentPath(); const auto pythonpath=qgetenv("PYTHONPATH");
        const auto restore=qScopeGuard([previous,pythonpath]{
            QDir::setCurrent(previous);
            if(pythonpath.isNull()) qunsetenv("PYTHONPATH"); else qputenv("PYTHONPATH",pythonpath);
        });
        QVERIFY(QDir().mkpath(temp.filePath("datahorizon")));
        QFile package(temp.filePath("datahorizon/__init__.py")); QVERIFY(package.open(QIODevice::WriteOnly));
        package.write("from pathlib import Path\nPath(__file__).with_name('executed').write_text('untrusted import')\nraise RuntimeError('untrusted import')\n"); package.close();
        QFile module(temp.filePath("json.py")); QVERIFY(module.open(QIODevice::WriteOnly));
        module.write("from pathlib import Path\nPath(__file__).with_name('json-executed').write_text('untrusted import')\nraise RuntimeError('untrusted import')\n"); module.close();
        QVERIFY(QDir::setCurrent(temp.path())); qputenv("PYTHONPATH",(temp.path()+QDir::listSeparator()).toUtf8());
        StartupOptions options; options.automaticWorkbook=false; options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_VERIFY_WITH_TIMEOUT(ready.count()==1 || QFile::exists(temp.filePath("datahorizon/executed")) || QFile::exists(temp.filePath("json-executed")),10000);
        QVERIFY(!QFile::exists(temp.filePath("datahorizon/executed"))); QVERIFY(!QFile::exists(temp.filePath("json-executed")));
        QCOMPARE(ready.count(),1);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("trusted.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14"),5000);
        QVERIFY(!QFile::exists(temp.filePath("datahorizon/executed"))); QVERIFY(!QFile::exists(temp.filePath("json-executed")));
    }
    void failedStartupCanClose() {
        const auto original=qgetenv("DATAHORIZON_PYTHON");
        qputenv("DATAHORIZON_PYTHON","/nonexistent/datahorizon-python");
        StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("startup failed"),5000);
        QVERIFY(window.close());
        if(original.isNull()) qunsetenv("DATAHORIZON_PYTHON"); else qputenv("DATAHORIZON_PYTHON",original);
    }
    void hiddenSectionsPersistMarkGapsAndGuardPaste() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.viewStatePath=temp.filePath("views.ini"); const auto path=temp.filePath("hidden.dhwb");
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{
                QJsonObject{{"row",1},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",1},{"source","1"}},
                QJsonObject{{"row",3},{"col",1},{"source","2"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            auto *grid=window.findChild<QTableView *>("grid"); grid->setRowHeight(1,66); grid->setColumnWidth(1,234);
            QSignalSpy mutations(&window,&MainWindow::operationFinished);
            grid->selectRow(1); window.findChild<QAction *>("hideRowsAction")->trigger(); QVERIFY(grid->isRowHidden(1));
            grid->selectColumn(1); window.findChild<QAction *>("hideColumnsAction")->trigger(); QVERIFY(grid->isColumnHidden(1));
            QVERIFY(window.model()->headerData(0,Qt::Horizontal,Qt::DisplayRole).toString().contains("⋯"));
            QVERIFY(window.model()->headerData(0,Qt::Vertical,Qt::DisplayRole).toString().contains("⋯"));
            QVERIFY(!grid->isColumnHidden(grid->currentIndex().column()));
            auto *show=window.findChild<QPushButton *>("showHiddenButton"); QVERIFY(show->isVisible());
            grid->setCurrentIndex(window.model()->index(0,0));
            auto *saved=new QMimeData;
            if(const auto *mime=QApplication::clipboard()->mimeData()) for(const auto &format:mime->formats()) saved->setData(format,mime->data(format));
            QApplication::clipboard()->setText("7\n8\n9");
            window.findChild<QAction *>("pasteAction")->trigger(); QApplication::clipboard()->setMimeData(saved);
            QVERIFY(mutations.isEmpty());
            window.exportCsvTo(temp.filePath("blocked.csv"),{{"r1",2},{"c1",1}});
            QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("export rectangle"));
            QVERIFY(!QFile::exists(temp.filePath("blocked.csv")));
            answerNextDialog(QMessageBox::Cancel); window.findChild<QAction *>("deleteRowsAction")->trigger();
            QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("structural edits"));
            auto *address=window.findChild<QLineEdit *>("address"); address->setText("B2"); QTest::keyClick(address,Qt::Key_Return);
            QCOMPARE(grid->currentIndex(),window.model()->index(0,0));
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false},
                {"sort",QJsonArray{QJsonObject{{"col",1},{"descending",false}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
            QCOMPARE(window.model()->viewRow(1),0); QVERIFY(grid->isRowHidden(0));
            grid->selectionModel()->select(QItemSelection(window.model()->index(0,0),window.model()->index(2,0)),QItemSelectionModel::ClearAndSelect);
            window.findChild<QAction *>("hideRowsAction")->trigger(); QVERIFY(!grid->isRowHidden(1)); QVERIFY(mutations.isEmpty());
        }
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,false,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid->isRowHidden(1)); QVERIFY(grid->isColumnHidden(1));
            window.findChild<QPushButton *>("showHiddenButton")->click();
            QVERIFY(!grid->isRowHidden(1)); QVERIFY(!grid->isColumnHidden(1));
            QCOMPARE(grid->rowHeight(1),66); QCOMPARE(grid->columnWidth(1),234);
            QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(1,0)).toString(),QString("1"),5000);
        }
    }
    void sheetDimensionsPersistAndFollowProjectedSourceRows() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.viewStatePath=temp.filePath("views.ini"); const auto path=temp.filePath("sizes.dhwb");
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{
                QJsonObject{{"row",1},{"col",1},{"source","3"}},QJsonObject{{"row",2},{"col",1},{"source","1"}},
                QJsonObject{{"row",3},{"col",1},{"source","2"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            auto *grid=window.findChild<QTableView *>("grid"); grid->setColumnWidth(0,234); grid->setRowHeight(2,86);
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false},
                {"sort",QJsonArray{QJsonObject{{"col",1},{"descending",false}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
            QCOMPARE(window.model()->sourceRow(1),2); QCOMPARE(grid->rowHeight(1),86); QCOMPARE(grid->columnWidth(0),234);
            QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);
            QSignalSpy mutations(&window,&MainWindow::operationFinished);
            grid->selectColumn(0); window.findChild<QAction *>("fitColumnsAction")->trigger();
            QVERIFY(grid->columnWidth(0)<234); QVERIFY(mutations.isEmpty());
            grid->setColumnWidth(0,234);
            window.submit({{"action","sheet_add"},{"name","Other"}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
            auto *tabs=window.findChild<QTabBar *>("sheets"); tabs->setCurrentIndex(1);
            QCOMPARE(grid->columnWidth(0),110); QCOMPARE(grid->rowHeight(2),26);
            tabs->setCurrentIndex(0);
            QCOMPARE(grid->columnWidth(0),234); QCOMPARE(grid->rowHeight(2),86);
        }
        {
            QSettings preferences(options.viewStatePath,QSettings::IniFormat);
            const auto key=preferences.allKeys().first();
            auto state=QJsonDocument::fromJson(preferences.value(key).toByteArray()).object();
            auto rows=state["rows"].toObject(); rows["-1"]=50; rows["1048576"]=50; rows["4"]=32.5;
            state["rows"]=rows; preferences.setValue(key,QJsonDocument(state).toJson(QJsonDocument::Compact));
        }
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,false,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid");
            QCOMPARE(grid->rowHeight(2),86); QCOMPARE(grid->rowHeight(1),26); QCOMPARE(grid->columnWidth(0),234);
            QCOMPARE(grid->rowHeight(4),26);
            grid->selectRow(2);
            QTimer::singleShot(20,[] { if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {dialog->setIntValue(77); dialog->accept();} });
            window.findChild<QAction *>("rowHeightAction")->trigger(); QCOMPARE(grid->rowHeight(2),77);
            window.findChild<QAction *>("resetDimensionsAction")->trigger();
            QCOMPARE(grid->rowHeight(2),26); QCOMPARE(grid->columnWidth(0),110);
        }
    }
    void cellBordersPaintAndClearWithoutChangingInput() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("border.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        const auto index=model->index(0,0); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(index),5000);
        auto render=[&]{
            QImage image(120,52,QImage::Format_ARGB32); image.fill(Qt::white); QPainter painter(&image);
            QStyleOptionViewItem option; option.initFrom(grid); option.rect=image.rect(); option.state=QStyle::State_Enabled;
            grid->itemDelegate()->paint(&painter,option,index); return image;
        };
        const auto before=render();
        window.findChild<QAction *>("allBordersAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(index),5000);
        const auto bordered=render(); QVERIFY(bordered.pixelColor(1,20)!=before.pixelColor(1,20));
        QCOMPARE(model->data(index,Qt::EditRole).toString(),QString());
        window.findChild<QAction *>("clearBordersAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000); QTRY_VERIFY_WITH_TIMEOUT(model->loaded(index),5000);
        QCOMPARE(render().pixelColor(1,20),before.pixelColor(1,20));
    }
    void wrappedTextRetainsInputAndFitsVisibleRows() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("wrap.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString text="A long text value that should occupy several lines in a narrow spreadsheet column";
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source",text},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        auto *wrap=window.findChild<QAction *>("wrapTextAction"); QVERIFY(wrap); wrap->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(0)>26,5000);
        QCOMPARE(model->data(model->index(0,0),Qt::EditRole).toString(),text);
        grid->scrollTo(model->index(10000,0),QAbstractItemView::PositionAtTop);
        QTRY_COMPARE_WITH_TIMEOUT(grid->rowHeight(0),26,5000);
        grid->scrollTo(model->index(0,0),QAbstractItemView::PositionAtTop);
        QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(0)>26,5000);
        const int narrowHeight=grid->rowHeight(0); grid->setColumnWidth(0,500);
        QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(0)<narrowHeight,5000);
        grid->setRowHeight(0,91); grid->setColumnWidth(0,110); QTest::qWait(30);
        QCOMPARE(grid->rowHeight(0),91);
        wrap->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(grid->rowHeight(0),91);
        window.findChild<QAction *>("resetDimensionsAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(grid->rowHeight(0),26,5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0),Qt::EditRole).toString(),text,5000);
    }
    void sourceEditorsPreserveLongInputs() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("long.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString original(40000,'x');
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source",original}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0),Qt::EditRole).toString(),original,5000);
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),original,5000);
        grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QCOMPARE(editor->text(),original);
        QCOMPARE(editor->accessibleName(),QString("Cell A1 input"));
        editor->setCursorPosition(editor->text().size()); editor->insert("z"); QTest::keyClick(editor,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000); grid->setCurrentIndex(model->index(0,0));
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),original+"z",5000);
        source->setModified(true); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0),Qt::EditRole).toString(),original+"z",5000);
    }
    void draftsRemainBoundToTheirOriginalCellAndSheet() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        auto *tabs=window.findChild<QTabBar *>("sheets");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        source->setFocus(); QTest::keyClicks(source,"draft"); grid->setFocus(); model->tileLoaded();
        QCOMPARE(source->text(),QString("draft")); QVERIFY(source->isModified());
        answerNextDialog(QMessageBox::Cancel); grid->setCurrentIndex(model->index(0,1));
        QCOMPARE(grid->currentIndex(),model->index(0,0)); QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Cancel); tabs->setCurrentIndex(1);
        QCOMPARE(tabs->currentIndex(),0); QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Cancel);
        for (auto *action:window.findChildren<QAction *>()) if (action->text().startsWith("Sort / filter selected list")) action->trigger();
        QCOMPARE(source->text(),QString("draft")); QVERIFY(source->isModified());
        answerNextDialog(QMessageBox::Save); grid->setCurrentIndex(model->index(0,1));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("draft"),5000);
        QCOMPARE(model->data(model->index(0,1)).toString(),QString());
    }
    void csvPreviewCapturesItsTargetAndRejectsInvalidatedProjection() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); const auto path=temp.filePath("import.dhwb"); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Yes); window.importCsvText("ID\n00123");
        grid->setCurrentIndex(model->index(4,1)); window.openBook(temp.filePath("wrong.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("00123"),5000);
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("ID")); QCOMPARE(model->data(model->index(4,1)).toString(),QString());
        QVERIFY(!QFile::exists(temp.filePath("wrong.dhwb")));
        window.importCsvText("wrong target"); model->setProjection({0,1});
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("CSV import target changed"),5000);
        QCOMPARE(changed.count(),1);
        window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("ID"),5000);
    }
    void csvImportResolvesActiveCellDraftBeforePreview() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("import-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        grid->edit(window.model()->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        QTest::keyClicks(editor,"retained input");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Cancel); window.importCsvText("replacement");
        QCOMPARE(changed.count(),0); QCOMPARE(opened.count(),1);
        QCOMPARE(source->text(),QString("retained input")); QVERIFY(source->isModified());
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
    }
    void csvFileSelectionCannotRetargetAfterExternalSheetDeletion() {
        const bool originalDialogs=QApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,true);
        const auto restoreDialogs=qScopeGuard([originalDialogs]{QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,originalDialogs);});
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto csvPath=temp.filePath("input.csv"); QFile csv(csvPath); QVERIFY(csv.open(QIODevice::WriteOnly));
        csv.write("wrong sheet"); csv.close();
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("import-file-target.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const auto original=window.model()->sheet();
        MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
        QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
        QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
        bool pickerOpened=false,refreshed=false,fileChosen=false;
        QElapsedTimer elapsed; elapsed.start();
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        connect(&writer,&MainWindow::workbookReady,&window,[&]{window.model()->externalChange();});
        connect(&window,&MainWindow::workbookReady,&window,[&]{
            if(window.model()->sheet()==original) return;
            refreshed=true;
            if(auto *dialog=qobject_cast<QFileDialog *>(QApplication::activeModalWidget())) {
                dialog->setDirectory(temp.path()); dialog->selectFile("input.csv");
            }
        });
        QTimer dialogs; dialogs.setInterval(10);
        connect(&dialogs,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QFileDialog *>(QApplication::activeModalWidget());dialog && !pickerOpened) {
                pickerOpened=true; writer.submit({{"action","sheet_delete"}});
            }
            if(auto *dialog=qobject_cast<QFileDialog *>(QApplication::activeModalWidget());dialog && refreshed && !fileChosen) {
                if(auto *name=dialog->findChild<QLineEdit *>("fileNameEdit")) name->setText(csvPath);
                auto *buttons=dialog->findChild<QDialogButtonBox *>();
                if(auto *open=buttons?buttons->button(QDialogButtonBox::Open):nullptr;open && open->isEnabled()) {
                    fileChosen=true; open->click();
                }
            }
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                if(auto *yes=box->button(QMessageBox::Yes)) yes->click();
                else if(auto *ok=box->button(QMessageBox::Ok)) ok->click();
            }
            if(elapsed.elapsed()>15000)
                if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget())) dialog->reject();
        });
        dialogs.start();
        QAction *import=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Import CSV…") import=action;
        QVERIFY(import); import->trigger();
        QVERIFY(pickerOpened); QVERIFY(refreshed); QVERIFY(fileChosen);
        auto *issues=window.findChild<QPlainTextEdit *>();
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("CSV import target changed") || changed.count()>0,10000);
        QCOMPARE(changed.count(),0);
        QVERIFY(issues->toPlainText().contains("CSV import target changed"));
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->loaded(window.model()->index(0,0)),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString());
    }
    void cutSpecialPasteDoesNotMoveInputs() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("cut.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","source"}},QJsonObject{{"row",1},{"col",2},{"source","destination"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("destination"),5000);
        auto *mime=new QMimeData;
        mime->setData("application/x-datahorizon-cells",QJsonDocument(QJsonObject{{"cut",true},{"source_workbook",model->workbook()},
            {"source_sheet",model->sheet()},{"r1",1},{"c1",1},{"r2",1},{"c2",1},{"cut_state",model->state()}}).toJson());
        QApplication::clipboard()->setMimeData(mime); grid->setCurrentIndex(model->index(0,1)); grid->setFocus();
        for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Paste formats only") action->trigger();
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("Cut supports full paste only"));
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("source")); QCOMPARE(model->data(model->index(0,1)).toString(),QString("destination"));
    }
    void newerCopySupersedesAnEarlierDelayedCut_data() {
        QTest::addColumn<bool>("cutFinishesFirst");
        QTest::newRow("older-reply-first") << true;
        QTest::newRow("newer-reply-first") << false;
    }
    void newerCopySupersedesAnEarlierDelayedCut() {
        QFETCH(bool,cutFinishesFirst);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clipboard-order.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","cut source"}},QJsonObject{{"row",1},{"col",2},{"source","copy source"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,1)),5000);
        DelayedClipboardServer server(model->state(),model->sheet()); QVERIFY(server.listen(QHostAddress::LocalHost,0));
        auto *client=window.findChild<RestClient *>();
        client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        int completed=0;
        connect(client->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply) {
            if(reply->url().path().endsWith("/clipboard")) ++completed;
        });
        QAction *cut=nullptr,*copy=nullptr;
        for(auto *action:window.findChildren<QAction *>()) {
            if(action->shortcut().matches(QKeySequence::Cut)==QKeySequence::ExactMatch) cut=action;
            if(action->shortcut().matches(QKeySequence::Copy)==QKeySequence::ExactMatch) copy=action;
        }
        QApplication::clipboard()->setText("prior clipboard");
        QVERIFY(cut); QVERIFY(copy); grid->setFocus(); cut->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);
        grid->setCurrentIndex(model->index(0,1)); copy->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,3000);
        QCOMPARE(server.requests[0].range["c1"].toInt(),1); QCOMPARE(server.requests[1].range["c1"].toInt(),2);
        const auto status=window.findChild<QLabel *>("storageStatus")->text();
        if(cutFinishesFirst) {
            server.respond(0,"cut source"); QTRY_COMPARE_WITH_TIMEOUT(completed,1,3000);
            QCOMPARE(QApplication::clipboard()->text(),QString("prior clipboard"));
            QCOMPARE(window.findChild<QLabel *>("storageStatus")->text(),status);
        }
        server.respond(1,"copy source"); QTRY_COMPARE_WITH_TIMEOUT(completed,cutFinishesFirst?2:1,3000);
        QCOMPARE(QApplication::clipboard()->text(),QString("copy source"));
        const auto copied=QApplication::clipboard()->mimeData()->data("application/x-datahorizon-cells");
        const auto payload=QJsonDocument::fromJson(copied).object();
        QVERIFY(!payload["cut"].toBool()); QCOMPARE(payload["source_workbook"].toString(),model->workbook());
        QCOMPARE(payload["source_sheet"].toString(),model->sheet()); QCOMPARE(payload["cut_state"].toString(),model->state());
        QCOMPARE(payload["origin_col"].toInt(),2);
        if(!cutFinishesFirst) { server.respond(0,"cut source"); QTRY_COMPARE_WITH_TIMEOUT(completed,2,3000); }
        QCOMPARE(QApplication::clipboard()->text(),QString("copy source"));
        QCOMPARE(QApplication::clipboard()->mimeData()->data("application/x-datahorizon-cells"),copied);
        QCOMPARE(window.findChild<QLabel *>("storageStatus")->text(),status);
    }
    void clipboardContentChangesSupersedePendingGridReplies_data() {
        QTest::addColumn<QString>("change"); QTest::addColumn<bool>("rejected");
        for(const auto &change:{QString("external"),QString("source-copy"),QString("source-cut")}) {
            QTest::newRow(qPrintable(change+"-success")) << change << false;
            QTest::newRow(qPrintable(change+"-error")) << change << true;
        }
    }
    void clipboardContentChangesSupersedePendingGridReplies() {
        QFETCH(QString,change); QFETCH(bool,rejected);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clipboard-change.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        DelayedClipboardServer server(model->state(),model->sheet()); QVERIFY(server.listen(QHostAddress::LocalHost,0));
        auto *client=window.findChild<RestClient *>();
        client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        int completed=0;
        connect(client->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply) {
            if(reply->url().path().endsWith("/clipboard")) ++completed;
        });
        QAction *cut=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut().matches(QKeySequence::Cut)==QKeySequence::ExactMatch) cut=action;
        QApplication::clipboard()->setText("before newer content");
        QVERIFY(cut); grid->setFocus(); cut->trigger(); QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);
        if(change=="external") QApplication::clipboard()->setText("new clipboard content");
        else {
            source->setFocus(); source->setText("new clipboard content"); source->selectAll();
            if(change=="source-copy") source->copy(); else source->cut();
        }
        QCOMPARE(QApplication::clipboard()->text(),QString("new clipboard content"));
        QVERIFY(!QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"));
        const auto status=window.findChild<QLabel *>("storageStatus")->text();
        auto *issues=window.findChild<QPlainTextEdit *>(); const auto previousIssues=issues->toPlainText();
        server.respond(0,"superseded cut",rejected); QTRY_COMPARE_WITH_TIMEOUT(completed,1,3000);
        QCOMPARE(QApplication::clipboard()->text(),QString("new clipboard content"));
        QVERIFY(!QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"));
        QCOMPARE(window.findChild<QLabel *>("storageStatus")->text(),status); QCOMPARE(issues->toPlainText(),previousIssues);
    }
    void clipboardLimitsRejectBeforeCellConstructionOrSubmission() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clipboard-limit.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","retained"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("retained"),5000);
        grid->setCurrentIndex(model->index(0,0)); grid->setFocus();
        QAction *paste=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut().matches(QKeySequence::Paste)==QKeySequence::ExactMatch) paste=action;
        QVERIFY(paste);
        auto *issues=window.findChild<QPlainTextEdit *>(); QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto reject=[&](QMimeData *mime,const QString &message) {
            issues->clear(); QApplication::clipboard()->setMimeData(mime); paste->trigger();
            return issues->toPlainText().contains(message) && changed.count()==0;
        };
        auto *largeText=new QMimeData; largeText->setText(QString(4*1024*1024+1,'x'));
        QVERIFY(reject(largeText,"4 MiB"));
        auto *manyFields=new QMimeData; manyFields->setText(QString(10000,'\t'));
        QVERIFY(reject(manyFields,"10,000-cell"));
        auto *largeNative=new QMimeData; largeNative->setData("application/x-datahorizon-cells",QByteArray(4*1024*1024+1,'x'));
        QVERIFY(reject(largeNative,"4 MiB"));
        QJsonArray row; for(int i=0;i<10001;++i) row.append(QJsonObject{{"source","x"}});
        auto *manyNative=new QMimeData;
        manyNative->setData("application/x-datahorizon-cells",QJsonDocument(QJsonObject{{"rows",QJsonArray{row}}}).toJson(QJsonDocument::Compact));
        QVERIFY(reject(manyNative,"10,000-cell"));
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("retained"));
    }
    void oversizedRequestsHaveAnAsynchronousKnownRejectionWithoutNetworkAccess() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        QSignalSpy connections(&server,&QTcpServer::newConnection);
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        bool called=false; QJsonObject receipt; QString message;
        client.request("/operations",{{"operation_id","oversized"},{"input",QString(4*1024*1024,'x')}},
                       [&](QJsonObject result,QString error){called=true;receipt=result;message=error;});
        QVERIFY(!called);
        QTRY_VERIFY_WITH_TIMEOUT(called,3000);
        QVERIFY(message.contains("4 MiB")); QVERIFY(RestClient::validReceipt(receipt,"oversized"));
        QCOMPARE(receipt["outcome"].toString(),QString("rejected")); QCOMPARE(connections.count(),0);
    }
    void localRequestsBypassProxiesAndNeverFollowRedirects() {
        QTcpServer origin, proxy;
        origin.setProxy(QNetworkProxy::NoProxy); proxy.setProxy(QNetworkProxy::NoProxy);
        QVERIFY(origin.listen(QHostAddress::LocalHost,0)); QVERIFY(proxy.listen(QHostAddress::LocalHost,0));
        QSignalSpy intercepted(&proxy,&QTcpServer::newConnection);
        auto serve=[this](QTcpServer &server,const QByteArray &response) {
            return connect(&server,&QTcpServer::newConnection,this,[&server,response] {
                auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
                connect(socket,&QTcpSocket::readyRead,socket,[socket,response] {
                    auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                    const auto end=request.indexOf("\r\n\r\n"); if(end<0 || socket->property("handled").toBool()) return;
                    const auto length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(request.left(end))).captured(1).toInt();
                    if(request.size()<end+4+length) return;
                    socket->setProperty("handled",true); socket->write(response); socket->disconnectFromHost();
                });
            });
        };
        serve(proxy,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}");
        const auto previous=QNetworkProxy::applicationProxy();
        auto restore=qScopeGuard([previous]{QNetworkProxy::setApplicationProxy(previous);});
        QNetworkProxy::setApplicationProxy(QNetworkProxy(QNetworkProxy::HttpProxy,"127.0.0.1",proxy.serverPort()));
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(origin.serverPort()),"private-test-token");
        auto direct=serve(origin,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: 11\r\n\r\n{\"ok\":true}");
        bool done=false; QJsonObject result; QString error;
        client.request("/direct",{},[&](QJsonObject value,QString message){result=value;error=message;done=true;},false);
        QTRY_VERIFY_WITH_TIMEOUT(done,3000);
        QCOMPARE(intercepted.count(),0); QVERIFY2(error.isEmpty(),qPrintable(error)); QVERIFY(result["ok"].toBool());
        disconnect(direct);
        const QByteArray fake="{\"operation_id\":\"redirected-operation\",\"outcome\":\"rolled_back\",\"code\":\"FAKE\",\"message\":\"Not a terminal receipt\"}";
        serve(origin,"HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:"+QByteArray::number(proxy.serverPort())+"/capture\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(fake.size())+"\r\n\r\n"+fake);
        done=false;
        client.request("/redirect",{{"operation_id","redirected-operation"},{"private","workbook contents"}},[&](QJsonObject value,QString message){result=value;error=message;done=true;});
        QTRY_VERIFY_WITH_TIMEOUT(done,3000);
        QCOMPARE(intercepted.count(),0); QVERIFY(!error.isEmpty()); QVERIFY(result.isEmpty());
    }
    void invalidHttpAcknowledgementsAreErrors() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        QByteArray payload;
        QByteArray status="200 OK";
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[socket,&payload,&status]{
                auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                if(!request.contains("\r\n\r\n")) return;
                socket->write("HTTP/1.1 "+status+"\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(payload.size())+"\r\n\r\n"+payload);
                socket->disconnectFromHost();
            });
        });
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(server.serverPort()),"test");
        for(const auto &body:{QByteArray("not json"),QByteArray("[]"),QByteArray("null"),QByteArray("{}")}) {
            payload=body; bool done=false; QString error; QJsonObject result;
            client.request("/receipt",{},[&](QJsonObject value,QString message){result=value;error=message;done=true;},false);
            QTRY_VERIFY_WITH_TIMEOUT(done,3000);
            if(body!="{}") QVERIFY(!error.isEmpty());
            QVERIFY(!RestClient::validReceipt(result,"operation",true));
        }
        status="500 Server Error"; payload="{\"message\":\"\"}";
        bool failed=false; QString transportError;
        client.request("/failure",{},[&](QJsonObject,QString error){failed=true;transportError=error;},false);
        QTRY_VERIFY_WITH_TIMEOUT(failed,3000); QVERIFY(!transportError.isEmpty());
        QJsonObject receipt{{"operation_id","operation"},{"outcome","committed"},{"state","state:1"},{"changed_cells",0}};
        QVERIFY(RestClient::validReceipt(receipt,"operation",true));
        QVERIFY(!RestClient::validReceipt(receipt,"other",true)); receipt.remove("state"); QVERIFY(!RestClient::validReceipt(receipt,"operation",true));
    }
    void genericMutationRejectionKeepsThePendingOperation() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("generic-rejection.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QTRY_VERIFY_WITH_TIMEOUT(window.model()->sourceReady(window.model()->index(0,0)),5000);
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0)); int mutations=0;
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[&,socket]{
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                if(!request.contains("\r\n\r\n") || socket->property("handled").toBool()) return;
                socket->setProperty("handled",true);
                const bool mutation=request.startsWith("POST ") && request.contains("/operations HTTP/");
                if(mutation) ++mutations;
                const QByteArray body=mutation
                    ? "{\"code\":\"STORAGE_ERROR\",\"message\":\"database is locked\",\"outcome\":\"rejected\"}"
                    : "{\"code\":\"OUTCOME_UNKNOWN\",\"message\":\"No confirmed receipt\",\"outcome\":\"unknown\"}";
                socket->write("HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
                socket->disconnectFromHost();
            });
        });
        window.findChild<RestClient *>()->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        const QJsonObject operation{{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}};
        window.submit(operation); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,5000); QVERIFY(!changed[0][0].toBool());
        QVERIFY(window.findChild<QLabel *>("storageStatus")->text().contains("Outcome unknown"));
        QVERIFY(!(window.model()->flags(window.model()->index(0,0))&Qt::ItemIsEditable));
        window.submit(operation); QCOMPARE(mutations,1);
        QDir recovery(options.recoveryDirectory); QCOMPARE(recovery.entryList({"operation-*.json"},QDir::Files).size(),1);
        answerNextDialog(QMessageBox::Yes); QVERIFY(window.close());
    }
    void windowsRecoveryPathsAreRecognized() {
        QVERIFY(MainWindow::isRecoveryPath("C:\\Users\\test\\DataHorizon\\recovery\\untitled.dhwb"));
        QVERIFY(MainWindow::isRecoveryPath("/tmp/recovery/untitled.dhwb"));
        QVERIFY(!MainWindow::isRecoveryPath("/tmp/ordinary.dhwb"));
    }
    void stoppedServiceCanRestartAndReopenStoredInputs() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false; options.recoveryDirectory=temp.filePath("recovery");
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("restart.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *process=window.findChild<QProcess *>("ownedService"); QVERIFY(process); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),2,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("7"),5000);
    }
    void unresolvedCrashCanCloseWithDurableRecoveryRecord() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false; options.recoveryDirectory=temp.filePath("recovery");
        options.handler=QString(DATAHORIZON_FIXTURES)+"/timeout_hook.py";
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); const auto path=temp.filePath("unknown.dhwb"); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QDir recovery(options.recoveryDirectory); QTRY_COMPARE_WITH_TIMEOUT(recovery.entryList({"operation-*.json"},QDir::Files).size(),1,5000);
        const auto recordPath=recovery.filePath(recovery.entryList({"operation-*.json"},QDir::Files).first());
        QFile record(recordPath); QVERIFY(record.open(QIODevice::ReadOnly)); const auto info=QJsonDocument::fromJson(record.readAll()).object(); record.close();
        QCOMPARE(QFileInfo(info["workbook_path"].toString()).canonicalFilePath(),QFileInfo(path).canonicalFilePath()); QVERIFY(!info["operation_id"].toString().isEmpty());
        answerNextDialog(QMessageBox::Yes); QVERIFY(window.close()); QVERIFY(QFile::exists(recordPath));
    }
    void initialWorkbookIsOpenedOnlyOnceAcrossServiceRestart() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto first=temp.filePath("first.dhwb"), second=temp.filePath("second.dhwb");
        {
            MainWindow seed(options); QSignalSpy ready(&seed,&MainWindow::serviceReady);
            QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&seed,&MainWindow::workbookReady); seed.openBook(first,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        }
        options.initialWorkbook=first;
        MainWindow window(options); window.show();
        QSignalSpy opened(&window,&MainWindow::workbookReady); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.openBook(second,true); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","second workbook"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("second workbook"),5000);
        QVERIFY(window.windowTitle().contains("second.dhwb"));
    }
    void restartPreservesDraftAndRequiresReviewOfReopenedInput() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        const auto path=temp.filePath("draft-restart.dhwb");
        MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","old input"},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        grid->setCurrentIndex(window.model()->index(2,1));
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("old input"),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"retained draft");
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QCOMPARE(source->text(),QString("retained draft"));
        {
            MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
            QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
            QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
            QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
            writer.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","changed while stopped"}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),2,10000);
        }
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),2,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QCOMPARE(grid->currentIndex(),window.model()->index(2,1)); QCOMPARE(source->text(),QString("retained draft")); QVERIFY(source->isModified());
        QSignalSpy changed(&window,&MainWindow::operationFinished); QString reviewText;
        answerNextDialog(QMessageBox::Cancel,&reviewText); QTest::keyClick(source,Qt::Key_Return);
        QVERIFY(reviewText.contains("changed while stopped")); QVERIFY(reviewText.contains("B3"));
        QCOMPARE(changed.count(),0); QCOMPARE(source->text(),QString("retained draft"));
        answerNextDialog(QMessageBox::Yes); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(2,1)).toString(),QString("retained draft"),5000);
        QCOMPARE(window.model()->cell(window.model()->index(2,1))["input_kind"].toString(),QString("text"));
    }
    void emptyListHasNoEditorTarget() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); const auto path=temp.filePath("empty-list.dhwb"); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","original"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *source=window.findChild<QLineEdit *>("source"); QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("original"),5000);
        window.showList({{"r1",1},{"r2",1},{"c1",1},{"c2",1},{"header",false},{"filters",QJsonArray{QJsonObject{{"col",1},{"op","="},{"value","no match"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(window.model()->rowCount(),0); QVERIFY(!source->isEnabled()); QVERIFY(source->text().isEmpty());
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        source->setText("hidden edit"); source->setModified(true); source->returnPressed();
        QCOMPARE(changed.count(),0);
        source->setModified(false); window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("original"),5000);
    }
    void copyAndCutSupportTenThousandCellSelections() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("bulk-copy.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","start"}},QJsonObject{{"row",10000},{"col",1},{"source","tail"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        grid->setCurrentIndex(model->index(0,0));
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(9999,0)),QItemSelectionModel::ClearAndSelect); grid->setFocus();
        QApplication::clipboard()->clear();
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut()==QKeySequence(QKeySequence::Copy)) action->trigger();
        auto clipboard=[] {const auto *mime=QApplication::clipboard()->mimeData(); return mime?QJsonDocument::fromJson(mime->data("application/x-datahorizon-cells")).object():QJsonObject{};};
        QTRY_COMPARE_WITH_TIMEOUT(clipboard()["rows"].toArray().size(),10000,5000);
        QCOMPARE(clipboard()["rows"].toArray().last().toArray().first().toObject()["source"].toString(),QString("tail"));
        for(auto *action:window.findChildren<QAction *>()) if(action->shortcut()==QKeySequence(QKeySequence::Cut)) action->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(clipboard()["cut"].toBool(),5000);
        QCOMPARE(clipboard()["rows"].toArray().size(),10000);
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("start"));
    }
    void longTextTilesRemainEditableWithoutOversizedReads() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("long-tile.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString text(64000,'x'); QJsonArray cells;
        for(int row=1;row<=32;++row) cells.append(QJsonObject{{"row",row},{"col",1},{"source",text},{"kind","text"}});
        window.submit({{"action","edit"},{"cells",cells}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(31,0)),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(31,0),Qt::EditRole).toString(),text,5000);
        grid->setCurrentIndex(model->index(31,0)); QTRY_COMPARE_WITH_TIMEOUT(source->text(),text,5000);
        QVERIFY(model->flags(model->index(31,0))&Qt::ItemIsEditable);
        source->setFocus(); source->setCursorPosition(source->text().size()); QTest::keyClicks(source,"y"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(31,0),Qt::EditRole).toString(),text+"y",5000);
        QVERIFY(!window.findChild<QPlainTextEdit *>()->toPlainText().contains("READ_LIMIT"));
    }
    void fullSourceReplyMustMatchViewportState() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[socket]{
                const auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                const int split=request.indexOf("\r\n\r\n"); if(split<0) return;
                const auto length=QRegularExpression("Content-Length: ([0-9]+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(request.left(split))).captured(1).toInt();
                if(request.size()<split+4+length) return;
                const bool preview=QJsonDocument::fromJson(request.mid(split+4,length)).object()["preview"].toBool();
                const auto body=QJsonDocument(QJsonObject{{"state",preview?"state:1":"state:2"},{"sheet","sheet"},
                    {"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source",preview?"preview":"different input"},{"source_omitted",preview},{"display","preview"}}}}}).toJson();
                socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body); socket->disconnectFromHost();
            });
        });
        RestClient client; client.configure("http://127.0.0.1:"+QString::number(server.serverPort()),"test");
        SheetModel model(&client,nullptr,32,16); model.bind("book","sheet","state:1",false); QSignalSpy changed(&model,&SheetModel::externalChange);
        model.data(model.index(0,0)); QTRY_VERIFY_WITH_TIMEOUT(model.loaded(model.index(0,0)),3000);
        model.ensureSource(model.index(0,0)); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,3000);
        QVERIFY(!model.sourceReady(model.index(0,0))); QVERIFY(!(model.flags(model.index(0,0))&Qt::ItemIsEditable));
    }
    void csvExportCannotReplaceNativeWorkbookOrItsAlias() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("retained.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        grid->setCurrentIndex(model->index(0,0));
        window.exportCsvTo(path); window.exportCsvTo(temp.path()+"/./retained.dhwb");
#ifdef Q_OS_UNIX
        const auto alias=temp.filePath("alias.csv"); QVERIFY(QFile::link(path,alias)); window.exportCsvTo(alias);
#endif
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("destination is the open native workbook"));
        QFile retained(path); QVERIFY(retained.open(QIODevice::ReadOnly)); QCOMPARE(retained.read(16),QByteArray("SQLite format 3\0",16)); retained.close();
        const auto csv=temp.filePath("values.csv"); window.exportCsvTo(csv);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(csv),5000);
        QFile output(csv); QVERIFY(output.open(QIODevice::ReadOnly)); QCOMPARE(output.readAll(),QByteArray("7\r\n"));
        window.openBook(path); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("7"),5000);
    }
    void unchangedEditorsPreserveNativeInputsWithoutRunningHooks_data() {
        QTest::addColumn<bool>("inlineEditor"); QTest::addColumn<QString>("inputKind"); QTest::addColumn<QString>("input");
        QTest::addColumn<bool>("textFormat"); QTest::addColumn<bool>("present"); QTest::addColumn<bool>("valuesPaste");
        for(bool inlineEditor:{false,true}) {
            const auto prefix=inlineEditor?QString("cell-"):QString("formula-bar-");
            QTest::newRow(qPrintable(prefix+"null")) << inlineEditor << QString("null") << QString() << false << true << false;
            QTest::newRow(qPrintable(prefix+"number-with-text-format")) << inlineEditor << QString("number") << QString("7") << true << true << false;
            QTest::newRow(qPrintable(prefix+"boolean")) << inlineEditor << QString("boolean") << QString("TRUE") << false << true << false;
            QTest::newRow(qPrintable(prefix+"formula-with-text-format")) << inlineEditor << QString("formula") << QString("=SUM(1,2)") << true << true << false;
            QTest::newRow(qPrintable(prefix+"auto-number-with-text-format")) << inlineEditor << QString("auto") << QString("7") << true << true << false;
            QTest::newRow(qPrintable(prefix+"text")) << inlineEditor << QString("text") << QString("00123") << false << true << false;
            QTest::newRow(qPrintable(prefix+"absence")) << inlineEditor << QString("auto") << QString() << false << false << false;
            QTest::newRow(qPrintable(prefix+"values-pasted-null")) << inlineEditor << QString("null") << QString() << false << true << true;
        }
    }
    void unchangedEditorsPreserveNativeInputsWithoutRunningHooks() {
        QFETCH(bool,inlineEditor); QFETCH(QString,inputKind); QFETCH(QString,input);
        QFETCH(bool,textFormat); QFETCH(bool,present); QFETCH(bool,valuesPaste);
        QTemporaryDir temp;
        const auto handler=temp.filePath("count_changes.py"); QFile code(handler); QVERIFY(code.open(QIODevice::WriteOnly));
        code.write("def on_change(context, changes):\n"
            "    for change in changes:\n"
            "        if (change['row'], change['col']) == (1, 1):\n"
            "            value = context.value(change['sheet'], 1, 4) or 0\n"
            "            context.set(change['sheet'], 1, 4, str(value + 1))\n"); code.close();
        StartupOptions options; options.automaticWorkbook=false; options.handler=handler;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("unchanged-input.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells{QJsonObject{{"row",1},{"col",4},{"source","0"}}};
        if(valuesPaste) cells.append(QJsonObject{{"row",2},{"col",1},{"source","=Z1"}});
        else if(present) cells.append(QJsonObject{{"row",1},{"col",1},{"source",input},{"kind",inputKind}});
        window.submit({{"action","edit"},{"cells",cells}}); int expectedOpened=2;
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        if(textFormat) {
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"style",QJsonObject{{"format","text"}}}}}}});
            ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        }
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        if(valuesPaste) {
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,0)),5000);
            QAction *copy=nullptr,*pasteValues=nullptr;
            for(auto *action:window.findChildren<QAction *>()) {
                if(action->shortcut().matches(QKeySequence::Copy)==QKeySequence::ExactMatch) copy=action;
                if(action->text()=="Paste values only") pasteValues=action;
            }
            QVERIFY(copy); QVERIFY(pasteValues); QApplication::clipboard()->clear();
            grid->setCurrentIndex(model->index(1,0)); grid->setFocus(); copy->trigger();
            QTRY_VERIFY_WITH_TIMEOUT(QApplication::clipboard()->mimeData()
                && QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"),5000);
            grid->setCurrentIndex(model->index(0,0)); pasteValues->trigger();
            ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        }
        grid->setCurrentIndex(model->index(0,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        const auto before=model->cell(model->index(0,0)); const auto state=model->state();
        const auto hookCount=model->cell(model->index(0,3))["value"].toDouble();
        QCOMPARE(before.contains("row"),present);
        if(present) { QCOMPARE(before["input_kind"].toString(),inputKind); QCOMPARE(before["source"].toString(),input); }
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        if(inlineEditor) {
            grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
            QVERIFY(!editor->isModified()); QTest::keyClick(editor,Qt::Key_Return); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        } else { source->setFocus(); QVERIFY(!source->isModified()); QTest::keyClick(source,Qt::Key_Return); }
        ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        QCOMPARE(changed.count(),1); QVERIFY(changed[0][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        QVERIFY(model->state()!=state); QCOMPARE(model->cell(model->index(0,0)),before);
        QCOMPARE(model->cell(model->index(0,3))["value"].toDouble(),hookCount);
        QCOMPARE(grid->currentIndex(),model->index(1,0));
        grid->setCurrentIndex(model->index(0,0)); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        if(inlineEditor) {
            grid->edit(model->index(0,0)); QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
            editor->selectAll(); QTest::keyClicks(editor,"9"); QTest::keyClick(editor,Qt::Key_Return);
        } else { source->setFocus(); source->selectAll(); QTest::keyClicks(source,"9"); QTest::keyClick(source,Qt::Key_Return); }
        ++expectedOpened; QTRY_COMPARE_WITH_TIMEOUT(opened.count(),expectedOpened,10000);
        QCOMPARE(changed.count(),2); QVERIFY(changed[1][0].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);
        const auto after=model->cell(model->index(0,0));
        QCOMPARE(after["source"].toString(),QString("9"));
        QCOMPARE(after["input_kind"].toString(),inputKind=="text" || textFormat?QString("text"):QString("auto"));
        QCOMPARE(after["kind"].toString(),inputKind=="text" || textFormat?QString("text"):QString("number"));
        QCOMPARE(model->cell(model->index(0,3))["value"].toDouble(),hookCount+1);
    }
    void guiEditsPreserveExplicitTextWhileAutoCellsStillInferTypes() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("typed-text.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","00123"},{"kind","text"}},
                                                          QJsonObject{{"row",2},{"col",1},{"source","auto text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("00123"),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"00124"); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("00124"),5000);
        QCOMPARE(model->cell(model->index(0,0))["kind"].toString(),QString("text"));
        QVERIFY(model->setData(model->index(0,0),"=1+1",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("=1+1"),5000);
        QCOMPARE(model->cell(model->index(0,0))["kind"].toString(),QString("text"));
        QVERIFY(model->setData(model->index(1,0),"7",Qt::EditRole));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("7"),5000);
        QCOMPARE(model->cell(model->index(1,0))["kind"].toString(),QString("number"));
    }
    void previousTransportSessionRepliesCannotReachCurrentUi() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        QPointer<QTcpSocket> delayed;
        auto reply=[](QTcpSocket *socket,const QByteArray &body){
            socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);
            socket->disconnectFromHost();
        };
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection(); connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[&,socket]{
                auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                if(!request.contains("\r\n\r\n") || socket->property("handled").toBool()) return;
                socket->setProperty("handled",true);
                if(request.startsWith("GET /old ")) delayed=socket;
                else reply(socket,"{\"value\":\"current\"}");
            });
        });
        RestClient client; const auto url="http://127.0.0.1:"+QString::number(server.serverPort()); client.configure(url,"old-token");
        QSignalSpy replies(client.findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished);
        bool staleCallback=false,currentCallback=false;
        client.request("/old",{},[&](QJsonObject,QString){staleCallback=true;},false);
        QTRY_VERIFY_WITH_TIMEOUT(delayed,3000);
        client.configure(url,"new-token");
        client.request("/new",{},[&](QJsonObject result,QString error){currentCallback=error.isEmpty() && result["value"]=="current";},false);
        QTRY_VERIFY_WITH_TIMEOUT(currentCallback,3000);
        reply(delayed,"{\"value\":\"stale\"}");
        QTRY_COMPARE_WITH_TIMEOUT(replies.count(),2,3000);
        QVERIFY(!staleCallback);
    }
    void inCellDraftSurvivesServiceRestartAtItsOriginalTarget() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("inline-draft.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","old"},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("old"),5000);
        grid->setCurrentIndex(model->index(2,1)); grid->edit(model->index(2,1));
        QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        editor->selectAll(); QTest::keyClicks(editor,"00124");
        answerNextDialog(QMessageBox::Cancel);
        for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Refresh stored data") action->trigger();
        QCOMPARE(source->text(),QString("00124")); QVERIFY(source->isModified());
        QTest::keyClick(source,Qt::Key_Escape);
        QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        grid->edit(model->index(2,1)); editor=grid->findChild<QLineEdit *>(); QVERIFY(editor);
        editor->selectAll(); QTest::keyClicks(editor,"00124");
        auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("00124"),5000); QVERIFY(source->isModified());
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000); QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000);
        QCOMPARE(grid->currentIndex(),model->index(2,1)); QCOMPARE(source->text(),QString("00124"));
        answerNextDialog(QMessageBox::Yes); QTest::keyClick(source,Qt::Key_Return);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("00124"),5000);
        QCOMPARE(model->cell(model->index(2,1))["kind"].toString(),QString("text"));
    }
    void reviewedOperationsCannotSilentlyUseNewStateOrSheet() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("reviewed-target.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto reviewed=window.model()->state(), sheet=window.model()->sheet();
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","new"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.submit({{"action","replace_all"},{"r1",1},{"r2",1},{"c1",1},{"c2",1},{"needle","new"},{"replacement","wrong"},{"expected_state",reviewed},{"sheet",sheet}});
        window.submit({{"action","edit"},{"sheet","different-sheet"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","wrong"}}}}});
        window.submit({{"action","edit"},{"workbook","different-workbook"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","wrong"}}}}});
        QCOMPARE(changed.count(),0);
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("Reviewed inputs changed"));
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("new"),5000);
    }
    void sheetDeletionConfirmationCannotSwitchToAnotherSheetAfterRefresh() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("confirmed-sheet.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","sheet_add"},{"name","Third"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        const auto original=window.model()->sheet();
        MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
        QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
        QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
        bool refreshed=false,confirmed=false;
        connect(&writer,&MainWindow::workbookReady,&window,[&]{window.model()->externalChange();});
        connect(&window,&MainWindow::workbookReady,&window,[&]{
            if(window.model()->sheet()==original) return;
            refreshed=true;
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                confirmed=true; box->button(QMessageBox::Yes)->click();
            }
        });
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        QTimer::singleShot(0,&writer,[&]{writer.submit({{"action","sheet_delete"}});});
        QTimer::singleShot(10000,&window,[]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) box->button(QMessageBox::No)->click();
        });
        QAction *remove=nullptr;
        for(auto *action:window.findChildren<QAction *>()) if(action->text()=="Delete sheet…") remove=action;
        QVERIFY(remove); remove->trigger();
        QVERIFY(refreshed); QVERIFY(confirmed);
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("Reviewed inputs changed"),5000);
        QCOMPARE(changed.count(),0);
        auto *tabs=window.findChild<QTabBar *>("sheets"); QCOMPARE(tabs->count(),2);
        QCOMPARE(tabs->tabText(0),QString("Other")); QCOMPARE(tabs->tabText(1),QString("Third"));
    }
    void missingDraftSheetRequiresExplicitDiscardBeforeNewEdits() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("missing-draft-sheet.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","old"},{"kind","text"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *source=window.findChild<QLineEdit *>("source");
        grid->setCurrentIndex(window.model()->index(2,1)); QTRY_COMPARE_WITH_TIMEOUT(source->text(),QString("old"),5000);
        source->setFocus(); source->selectAll(); QTest::keyClicks(source,"retained draft");
        const auto original=window.model()->sheet(); auto *process=window.findChild<QProcess *>("ownedService"); process->kill();
        QTRY_COMPARE_WITH_TIMEOUT(process->state(),QProcess::NotRunning,5000);
        {
            MainWindow writer(options); QSignalSpy writerReady(&writer,&MainWindow::serviceReady);
            QTRY_COMPARE_WITH_TIMEOUT(writerReady.count(),1,10000);
            QSignalSpy writerOpened(&writer,&MainWindow::workbookReady); writer.openBook(path);
            QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),1,10000);
            writer.submit({{"action","sheet_delete"}}); QTRY_COMPARE_WITH_TIMEOUT(writerOpened.count(),2,10000);
        }
        window.findChild<QAction *>("restartServiceAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QVERIFY(window.model()->sheet()!=original);
        QCOMPARE(source->text(),QString("retained draft")); QVERIFY(source->isModified());
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        source->returnPressed(); QCOMPARE(changed.count(),0);
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("The draft target changed"));
        answerNextDialog(QMessageBox::Cancel);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","new"}}}}});
        QCOMPARE(changed.count(),0); QCOMPARE(source->text(),QString("retained draft"));
        QTRY_VERIFY_WITH_TIMEOUT(source->isEnabled(),5000); QTest::keyClick(source,Qt::Key_Escape);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","new"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("new"),5000);
    }
private:
    static void observeOwnedServiceStartup(MainWindow &owner, QJsonObject &service) {
        auto *process=owner.findChild<QProcess *>("ownedService");
        connect(process,&QProcess::readyReadStandardOutput,process,[process,&service,bytes=QByteArray{}]() mutable {
            if(!service.isEmpty()) return;
            bytes+=process->peek(process->bytesAvailable());
            const auto newline=bytes.indexOf('\n');
            if(newline>=0) service=QJsonDocument::fromJson(bytes.left(newline)).object();
        });
    }
    static void answerNextDialog(QMessageBox::StandardButton button, QString *message=nullptr) {
        auto *timer=new QTimer(qApp); timer->setInterval(10);
        connect(timer,&QTimer::timeout,timer,[timer,button,message]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                if(message) *message=box->text();
                if(auto *target=box->button(button)) { timer->stop(); target->click(); timer->deleteLater(); }
            }
        });
        timer->start(); QTimer::singleShot(5000,timer,&QObject::deleteLater);
    }
};

QTEST_MAIN(DesktopTests)
#include "test_desktop.moc"
