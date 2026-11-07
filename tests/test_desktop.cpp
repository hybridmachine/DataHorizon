// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include "servicepaths.h"
#include "frozen_row_probe.h"
#include "frozentoprow.h"
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
#include "listcriteriadialog.h"
#include "namedlistviews.h"
#include <QSettings>
#include <QAbstractButton>
#include <QPushButton>
#include <QProgressDialog>
#include <QAbstractItemDelegate>
#include <QClipboard>
#include <QMenu>
#include <QComboBox>
#include <QCheckBox>
#include <QSpinBox>
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

class DelayedSummaryServer : public QTcpServer {
public:
    struct Pending {QPointer<QTcpSocket> socket;QJsonObject range;};
    QList<Pending> requests,navigationRequests;
    QString state,sheet;
    DelayedSummaryServer(QString version,QString source):state(version),sheet(source) {
        connect(this,&QTcpServer::newConnection,this,[this]{
            auto *socket=nextPendingConnection();connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[this,socket]{
                const auto bytes=socket->property("request").toByteArray()+socket->readAll();socket->setProperty("request",bytes);
                const int end=bytes.indexOf("\r\n\r\n");if(end<0 || socket->property("handled").toBool())return;
                const int length=QRegularExpression("Content-Length: (\\d+)",QRegularExpression::CaseInsensitiveOption).match(QString::fromLatin1(bytes.left(end))).captured(1).toInt();
                if(bytes.size()<end+4+length)return;socket->setProperty("handled",true);
                if(bytes.contains("/navigate HTTP/"))navigationRequests.append({socket,QJsonDocument::fromJson(bytes.mid(end+4)).object()});
                else if(bytes.contains("/summary HTTP/"))requests.append({socket,QJsonDocument::fromJson(bytes.mid(end+4)).object()});
                else send(socket,{{"state",state},{"sheet",sheet},{"cells",QJsonArray{}}});
            });
        });
    }
    void respond(int index,double sum) {
        const auto &pending=requests.at(index);const auto &r=pending.range;
        send(pending.socket,{{"state",state},{"sheet",sheet},{"range",QJsonArray{r["r1"],r["c1"],r["r2"],r["c2"]}},
            {"count",1},{"numeric_count",1},{"selected_cells",1},{"sum",sum},{"average",sum},{"error_count",0},{"numeric_error",QJsonValue::Null}});
    }
    void respondNavigation(int index,int row,int col) {send(navigationRequests.at(index).socket,{{"state",state},{"sheet",sheet},{"row",row},{"col",col},{"basis","stored_inputs"}});}
    void respondInvalid(int index) {send(requests.at(index).socket,{{"state",state},{"sheet",sheet}});}
private:
    static void send(QTcpSocket *socket,QJsonObject result) {
        const auto body=QJsonDocument(result).toJson(QJsonDocument::Compact);
        socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(body.size())+"\r\n\r\n"+body);socket->disconnectFromHost();
    }
};

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
                if(request.startsWith("POST ") && (request.contains("/clipboard HTTP/") || request.contains("/list/values HTTP/")))
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
    void respondList(int index,const QString &text) {send(requests.at(index).socket,{{"state",m_state},{"rows",QJsonArray{QJsonArray{QJsonObject{{"source",text},{"display",text},{"input_kind","text"}}}}}});}
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
    void frozenTopRowModelForwardsEditsAndTracksSourceResets() {
        QStandardItemModel source(3,4); source.setData(source.index(0,0),"Header");
        FrozenTopRowModel first(&source); QAbstractItemModelTester tester(&first,QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(first.rowCount(),1); QVERIFY(first.flags(first.index(0,0))&Qt::ItemIsEditable);
        QVERIFY(first.setData(first.index(0,0),"Edited",Qt::EditRole)); QCOMPARE(source.data(source.index(0,0)).toString(),QString("Edited"));
        source.insertRow(0); source.setData(source.index(0,0),"Inserted"); QCOMPARE(first.data(first.index(0,0)).toString(),QString("Inserted"));
        source.removeRows(0,source.rowCount()); QCOMPARE(first.rowCount(),0);
        auto *shortLived=new QStandardItemModel(1,4);
        FrozenTopRowModel detached(shortLived); QAbstractItemModelTester detachedTester(&detached,QAbstractItemModelTester::FailureReportingMode::QtTest);
        const auto stale=detached.index(0,0); delete shortLived;
        QCOMPARE(detached.rowCount(),0); QCOMPARE(detached.columnCount(),0);
        QVERIFY(!detached.data(stale).isValid()); QVERIFY(!detached.setData(stale,"ignored",Qt::EditRole));
    }
    void frozenTopRowEditsAlignsAndPersistsWithoutCoveringBody() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        options.viewStatePath=temp.filePath("views.ini"); const auto path=temp.filePath("frozen.dhwb");
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=SUM(7,13.5)"},{"style",QJsonObject{{"border","box"}}}}}}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
            auto *freeze=window.findChild<QAction *>("freezeTopRowAction"); freeze->trigger();
            auto *top=window.findChild<QTableView *>("frozenTopRow"); QVERIFY(top->isVisible()); QVERIFY(grid->isRowHidden(0));
            QVERIFY(top->viewport()->mapToGlobal(QPoint(0,top->rowHeight(0))).y()<=grid->viewport()->mapToGlobal(QPoint()).y());
            grid->scrollTo(model->index(10000,7),QAbstractItemView::PositionAtTop);
            QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(10000,7)),5000);
            grid->setColumnWidth(7,188); QCOMPARE(top->columnWidth(7),188);
            QCOMPARE(top->viewport()->mapToGlobal(top->visualRect(top->model()->index(0,7)).topLeft()).x(),
                     grid->viewport()->mapToGlobal(grid->visualRect(model->index(10000,7)).topLeft()).x());
            const int scroll=grid->verticalScrollBar()->value(); top->setCurrentIndex(top->model()->index(0,7));
            QCOMPARE(grid->currentIndex(),model->index(0,7)); QCOMPARE(grid->verticalScrollBar()->value(),scroll);
            top->setFocus(); QTest::keyClick(top,Qt::Key_Right,Qt::ShiftModifier);
            QCOMPARE(grid->selectionModel()->selectedIndexes().size(),2);
            top->setCurrentIndex(top->model()->index(0,7));
            QSignalSpy edits(model,&SheetModel::editRequested);
            top->edit(top->model()->index(0,7)); QPointer<QLineEdit> editor=top->findChild<QLineEdit *>(); QVERIFY(editor);
            editor->setText("999"); QTest::keyClick(editor,Qt::Key_Escape); QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000); QVERIFY(edits.isEmpty());
            top->edit(top->model()->index(0,7)); editor=top->findChild<QLineEdit *>(); QVERIFY(editor);
            QCOMPARE(editor->accessibleName(),QString("Cell H1 input")); editor->setText("123"); QTest::keyClick(editor,Qt::Key_Return);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000); QTRY_COMPARE_WITH_TIMEOUT(edits.count(),1,5000);
            QTRY_COMPARE_WITH_TIMEOUT(top->model()->data(top->model()->index(0,7)).toString(),QString("123"),5000);
            QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(1,7),5000);
            QTest::keyClick(grid,Qt::Key_Up); QCOMPARE(grid->currentIndex(),model->index(0,7));
            QTest::keyClick(top,Qt::Key_Down); QCOMPARE(grid->currentIndex(),model->index(1,7));
            QTest::mouseClick(top->horizontalHeader()->viewport(),Qt::LeftButton,Qt::NoModifier,
                QPoint(top->horizontalHeader()->sectionViewportPosition(7)+10,5));
            QCOMPARE(grid->selectionModel()->selection().first().height(),model->rowCount());
            top->setRowHeight(0,77); QCOMPARE(top->rowHeight(0),77);
            grid->selectColumn(2); window.findChild<QAction *>("hideColumnsAction")->trigger(); QVERIFY(top->isColumnHidden(2));
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",8},{"header",false}});
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000); QVERIFY(!top->isVisible());
            window.findChild<QAction *>("clearListAction")->trigger();
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000); QVERIFY(top->isVisible());
            QVERIFY(model->cacheMetrics()["tiles"].toInt()<=16);
        }
        {
            MainWindow window(options); window.show();
            QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,false,true);
            QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *top=window.findChild<QTableView *>("frozenTopRow"); auto *grid=window.findChild<QTableView *>("grid");
            QVERIFY(top->isVisible()); QVERIFY(top->isColumnHidden(2));
            QCOMPARE(top->rowHeight(0),77);
            QVERIFY(!(top->model()->flags(top->model()->index(0,7))&Qt::ItemIsEditable));
            QTRY_COMPARE_WITH_TIMEOUT(top->model()->data(top->model()->index(0,7)).toString(),QString("123"),5000);
            window.findChild<QAction *>("freezeTopRowAction")->trigger(); QVERIFY(!top->isVisible()); QVERIFY(!grid->isRowHidden(0));
            QCOMPARE(grid->rowHeight(0),77);
        }
    }
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
            QTimer cancelUnexpectedDialog; cancelUnexpectedDialog.setInterval(10);
            connect(&cancelUnexpectedDialog,&QTimer::timeout,[]{
                if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) if(auto *cancel=box->button(QMessageBox::Cancel)) cancel->click();
            });
            cancelUnexpectedDialog.start(); window.findChild<QAction *>("deleteRowsAction")->trigger(); cancelUnexpectedDialog.stop();
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
    void sheetRenameDialogRetainsIdentityAndUpdatesFormulaReferences() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("rename.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto id=window.model()->sheet();
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=Sheet1!A2"}},
            QJsonObject{{"row",2},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                dialog->setTextValue("O'Brien"); dialog->accept();
            }
        });
        answer.start(); window.findChild<QAction *>("renameSheetAction")->trigger(); answer.stop();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QCOMPARE(window.model()->sheet(),id);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->cell(window.model()->index(0,0))["source"].toString(),QString("='O''Brien'!A2"),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString("7"));
    }
    void sheetDuplicateDialogCopiesSelfReferencesWithoutChangingSource() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("duplicate.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto original=window.model()->sheet();
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=Sheet1!A2"}},
            QJsonObject{{"row",2},{"col",1},{"source","9"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                dialog->setTextValue("Copy"); dialog->accept();
            }
        });
        answer.start(); window.findChild<QAction *>("duplicateSheetAction")->trigger(); answer.stop();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        auto *tabs=window.findChild<QTabBar *>(); QCOMPARE(tabs->count(),2); tabs->setCurrentIndex(1);
        QVERIFY(window.model()->sheet()!=original);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->cell(window.model()->index(0,0))["source"].toString(),QString("='Copy'!A2"),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString("9"));
    }
    void sheetDeleteDialogWarnsAndReportsBrokenReferences() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("delete.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","sheet_add"},{"name","Other"}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *tabs=window.findChild<QTabBar *>(); tabs->setCurrentIndex(1);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=Sheet1!A2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        tabs->setCurrentIndex(0);
        bool warned=false; QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                warned=box->text().contains("#REF!") && box->text().contains("Undo");
                box->button(QMessageBox::Yes)->click();
            }
        });
        answer.start(); window.findChild<QAction *>("deleteSheetAction")->trigger(); answer.stop();
        QVERIFY(warned); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QCOMPARE(tabs->count(),1);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("#REF!"),5000);
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("Other!A1"));
    }
    void namedListCriteriaValidationRejectsMalformedPreferencesWithoutClamping() {
        QJsonObject output;const QJsonObject good{{"r1",1},{"r2",4},{"c1",1},{"c2",2},{"header",false},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}},{"filters",QJsonArray{QJsonObject{{"col",2},{"op","="},{"value",QJsonValue::Null}},QJsonObject{{"col",1},{"op","="},{"value",""}}}}};
        QVERIFY(normalizedListView(good,output));QVERIFY(output["filters"].toArray()[0].toObject()["value"].isNull());QVERIFY(output["filters"].toArray()[1].toObject()["value"].isString());
        for(const auto &change:{QJsonObject{{"r2",1048576}},QJsonObject{{"c1",1.5}},QJsonObject{{"sort",QJsonValue::Null}},QJsonObject{{"header",1}},QJsonObject{{"workbook","old-handle"}},QJsonObject{{"filters",QJsonArray{QJsonObject{{"col",3},{"op","="},{"value",1}}}}},QJsonObject{{"filters",QJsonArray{QJsonObject{{"col",1},{"op","contains"},{"value",false}}}}}}){auto invalid=good;for(auto it=change.begin();it!=change.end();++it)invalid[it.key()]=it.value();QVERIFY(!normalizedListView(invalid,output));}
        QVERIFY(validListViewName("High quantity"));QVERIFY(!validListViewName(" High"));QVERIFY(!validListViewName(QString(65,'x')));QVERIFY(!validListViewName("line\nbreak"));
    }
    void namedListViewsRejectOversizeWithoutReplacingSavedPreferences() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("bytes.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.showList({{"r1",1},{"r2",1},{"c1",1},{"c2",1}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);window.saveNamedListView("Small");QJsonArray filters;for(int i=0;i<3;++i)filters.append(QJsonObject{{"col",1},{"op","="},{"value",QString(50000,'x')}});
        window.showList({{"r1",1},{"r2",1},{"c1",1},{"c2",1},{"filters",filters}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);window.saveNamedListView("Too large");QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("128 KiB"));QSettings settings(options.viewStatePath,QSettings::IniFormat);const auto saved=QJsonDocument::fromJson(settings.value(settings.allKeys().first()).toByteArray()).object()["named_views"].toObject();QCOMPARE(saved.size(),1);QVERIFY(saved.contains("Small"));
    }
    void namedListViewsPersistReopenReviewAndReevaluateCurrentInputsReadOnly() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");const auto path=temp.filePath("named.dhwb");QString key;
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","ID"}},QJsonObject{{"row",1},{"col",2},{"source","Qty"}},QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","2"}},QJsonObject{{"row",3},{"col",1},{"source","Z"}},QJsonObject{{"row",3},{"col",2},{"source","3"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true},{"sort",QJsonArray{QJsonObject{{"col",2},{"descending",true}}}},{"filters",QJsonArray{QJsonObject{{"col",2},{"op",">"},{"value",1}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QSignalSpy changes(&window,&MainWindow::operationFinished);
            window.saveNamedListView("High qty");for(int i=0;i<15;++i)window.saveNamedListView("View "+QString::number(i));window.saveNamedListView("Overflow");QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("16 names"));window.saveNamedListView("High qty");QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("already exists"));window.removeNamedListView("View 0");window.saveNamedListView("Replacement");QVERIFY(changes.isEmpty());
            QSettings settings(options.viewStatePath,QSettings::IniFormat);key=settings.allKeys().first();const auto saved=QJsonDocument::fromJson(settings.value(key).toByteArray()).object()["named_views"].toObject();QCOMPARE(saved.size(),16);QVERIFY(!saved.contains("Overflow"));QVERIFY(saved.contains("Replacement"));const auto criteria=saved["High qty"].toObject();QVERIFY(!criteria.contains("expected_state"));QVERIFY(!criteria.contains("workbook"));QCOMPARE(criteria["r2"].toInt(),3);
            window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","0"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);window.findChild<QAction *>("clearListAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
        }
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);QSignalSpy changes(&window,&MainWindow::operationFinished);
            QTimer::singleShot(0,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());QVERIFY(dialog);QCOMPARE(dialog->objectName(),QString("listCriteriaDialog"));QVERIFY(dialog->windowTitle().contains("review source coordinates"));dialog->reject();});window.applyNamedListView("High qty");QVERIFY(!window.model()->projected());
            QTimer::singleShot(0,&window,[&]{auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());QVERIFY(dialog);QVERIFY(dialog->findChild<QCheckBox *>("listHeader")->isChecked());QCOMPARE(dialog->findChild<QTableWidget *>("filterCriteria")->rowCount(),1);dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();});window.applyNamedListView("High qty");QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QVERIFY(window.model()->projected());QCOMPARE(window.model()->rowCount(),2);QCOMPARE(window.model()->sourceRow(1),2);QVERIFY(changes.isEmpty());
            window.removeNamedListView("High qty");QVERIFY(window.model()->projected());QSettings settings(options.viewStatePath,QSettings::IniFormat);QVERIFY(!QJsonDocument::fromJson(settings.value(key).toByteArray()).object()["named_views"].toObject().contains("High qty"));
        }
        {QSettings settings(options.viewStatePath,QSettings::IniFormat);auto state=QJsonDocument::fromJson(settings.value(key).toByteArray()).object();auto views=state["named_views"].toObject();views["Broken"]=QJsonObject{{"r1",1},{"r2",1000000},{"c1",1},{"c2",10}};state["named_views"]=views;settings.setValue(key,QJsonDocument(state).toJson(QJsonDocument::Compact));settings.sync();}
        {MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("Invalid saved list view"));window.applyNamedListView("Broken");QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("unavailable or invalid"));}
    }
    void filteredExportWritesOnlyReviewedVisibleSnapshotAndRetainsDestinationOnCancelError() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);
        const auto native=temp.filePath("list-export.dhwb");window.openBook(native,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","ID"}},QJsonObject{{"row",1},{"col",2},{"source","Value"}},QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","=1/4"},{"style",QJsonObject{{"format","percent2"}}}},QJsonObject{{"row",3},{"col",1},{"source","Z"}},QJsonObject{{"row",3},{"col",2},{"source","TRUE"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QSignalSpy edits(&window,&MainWindow::operationFinished);
        bool accept=true;int reviews=0;QTimer timer;connect(&timer,&QTimer::timeout,&window,[&]{if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget()))if(box->objectName()=="listExportReview"){QVERIFY(box->informativeText().contains("formula inputs converted"));++reviews;box->button(accept?QMessageBox::Yes:QMessageBox::Cancel)->click();}});timer.start(10);
        for(const auto &format:{QString("csv"),QString("xlsx"),QString("ods")}){const auto path=temp.filePath("result."+format);window.exportListTo(path,format);QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(path),5000);QFile file(path);QVERIFY(file.open(QIODevice::ReadOnly));const auto bytes=file.readAll();if(format=="csv")QCOMPARE(bytes,QByteArray("ID,Value\r\nZ,TRUE\r\n00123,0.25\r\n"));else QVERIFY(bytes.startsWith("PK"));}
        QCOMPARE(reviews,3);QVERIFY(edits.isEmpty());const auto keep=temp.filePath("keep.csv");{QFile file(keep);QVERIFY(file.open(QIODevice::WriteOnly));file.write("keep");}
        accept=false;window.exportListTo(keep);QTRY_COMPARE_WITH_TIMEOUT(reviews,4,5000);QTest::qWait(20);{QFile file(keep);QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("keep"));}
        window.exportListTo(native);QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("destination is the native workbook"));
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",2},{"source","=1/0"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);window.exportListTo(keep);QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("List values blocked"),5000);QCOMPARE(reviews,4);QFile file(keep);QVERIFY(file.open(QIODevice::ReadOnly));QCOMPARE(file.readAll(),QByteArray("keep"));
    }
    void filteredCopyUsesDisplayedValuesAndStylesAndPreservesClipboardOnEmpty() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("list-copy.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","ID"}},QJsonObject{{"row",1},{"col",2},{"source","Value"}},
            QJsonObject{{"row",2},{"col",1},{"source","00123"},{"kind","text"}},QJsonObject{{"row",2},{"col",2},{"source","=1/4"},{"style",QJsonObject{{"format","percent2"}}}},
            QJsonObject{{"row",3},{"col",1},{"source","Z"}},QJsonObject{{"row",3},{"col",2},{"source","TRUE"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();QSignalSpy edits(&window,&MainWindow::operationFinished);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true},{"sort",QJsonArray{QJsonObject{{"col",1},{"descending",true}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QApplication::clipboard()->setText("before");window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(QApplication::clipboard()->text(),QString("ID\tValue\nZ\tTRUE\n00123\t25.00%"),5000);QVERIFY(edits.isEmpty());
        const auto payload=QJsonDocument::fromJson(QApplication::clipboard()->mimeData()->data("application/x-datahorizon-cells")).object();auto copied=payload["rows"].toArray();QCOMPARE(copied[2].toArray()[1].toObject()["input_kind"].toString(),QString("number"));QCOMPARE(copied[2].toArray()[1].toObject()["value"].toDouble(),.25);
        grid->selectionModel()->select(QItemSelection(model->index(1,0),model->index(2,1)),QItemSelectionModel::ClearAndSelect);grid->setFocus();QAction *copy=nullptr;for(auto *action:window.findChildren<QAction *>())if(action->shortcut().matches(QKeySequence::Copy)==QKeySequence::ExactMatch)copy=action;QVERIFY(copy);copy->trigger();QTRY_COMPARE_WITH_TIMEOUT(QApplication::clipboard()->text(),QString("Z\tTRUE\n00123\t25.00%"),5000);
        window.findChild<QAction *>("clearListAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);grid->setCurrentIndex(model->index(9,0));grid->setFocus();QTest::keySequence(grid,QKeySequence::Paste);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(10,1)),5000);QCOMPARE(model->cell(model->index(10,0))["input_kind"].toString(),QString("text"));QCOMPARE(model->cell(model->index(10,0))["source"].toString(),QString("00123"));QCOMPARE(model->cell(model->index(10,1))["source"].toString(),QString("0.25"));QCOMPARE(model->cell(model->index(10,1))["style"].toObject()["format"].toString(),QString("percent2"));
        grid->setCurrentIndex(model->index(1,0));grid->selectionModel()->select(QItemSelection(model->index(1,0),model->index(1,1)),QItemSelectionModel::ClearAndSelect);QVERIFY(!grid->selectionModel()->selection().isEmpty());window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY2(grid->isRowHidden(1),qPrintable(window.findChild<QPlainTextEdit *>()->toPlainText()));window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",true}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(QApplication::clipboard()->text(),QString("ID\tValue\nZ\tTRUE"),5000);
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"filters",QJsonArray{QJsonObject{{"col",1},{"op","="},{"value","missing"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);const auto prior=QApplication::clipboard()->text();window.copyListValues();QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("No visible list values"),5000);QCOMPARE(QApplication::clipboard()->text(),prior);
    }
    void filteredCopyIgnoresSupersededClipboardAndServiceReplies() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("late-list.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);window.showList({{"r1",1},{"r2",2},{"c1",1},{"c2",1}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();DelayedClipboardServer server(model->state(),model->sheet());QVERIFY(server.listen(QHostAddress::LocalHost,0));auto *client=window.findChild<RestClient *>();client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);QApplication::clipboard()->setText("new external clipboard");server.respondList(0,"stale");QTest::qWait(100);QCOMPARE(QApplication::clipboard()->text(),QString("new external clipboard"));
        window.copyListValues();QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,3000);client->configure("http://127.0.0.1:1","other");server.respondList(1,"wrong service");QTest::qWait(100);QCOMPARE(QApplication::clipboard()->text(),QString("new external clipboard"));
    }
    void multiCriteriaEditorPreservesScalarTypesOrderAndLimits() {
        QJsonArray keys;for(int col=1;col<=8;++col)keys.append(QJsonObject{{"col",col},{"descending",col==8}});
        const QJsonArray filters{QJsonObject{{"col",1},{"op","="},{"value","00123"}},QJsonObject{{"col",2},{"op",">"},{"value",1.25}},
            QJsonObject{{"col",3},{"op","="},{"value",false}},QJsonObject{{"col",4},{"op","="}},QJsonObject{{"col",5},{"op","="},{"value",""}}};
        ListCriteriaDialog dialog({{"r1",1},{"c1",1},{"r2",10},{"c2",8},{"header",false},{"sort",keys},{"filters",filters}});
        auto *sort=dialog.findChild<QTableWidget *>("sortCriteria"),*table=dialog.findChild<QTableWidget *>("filterCriteria");
        QCOMPARE(sort->rowCount(),8);QVERIFY(!dialog.findChild<QPushButton *>("addSort")->isEnabled());
        sort->setCurrentCell(7,0);dialog.findChild<QPushButton *>("sortUp")->click();QCOMPARE(qobject_cast<QSpinBox *>(sort->cellWidget(6,0))->value(),8);
        QCOMPARE(qobject_cast<QComboBox *>(sort->cellWidget(6,1))->currentIndex(),1);
        for(int i=0;i<4;++i)dialog.findChild<QPushButton *>("addFilter")->click();QCOMPARE(table->rowCount(),8);QVERIFY(!dialog.findChild<QPushButton *>("addFilter")->isEnabled());
        table->setCurrentCell(7,0);dialog.findChild<QPushButton *>("removeFilter")->click();QVERIFY(dialog.findChild<QPushButton *>("addFilter")->isEnabled());
        auto *number=qobject_cast<QLineEdit *>(table->cellWidget(1,3));number->setText("not a number");
        auto *ok=dialog.findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok);ok->click();QVERIFY(dialog.result()!=QDialog::Accepted);QVERIFY(dialog.findChild<QLabel *>("criteriaError")->text().contains("finite"));
        number->setText("2.5");ok->click();QCOMPARE(dialog.result(),int(QDialog::Accepted));const auto result=dialog.scope();
        QCOMPARE(result["filters"].toArray()[0].toObject()["value"].toString(),QString("00123"));QCOMPARE(result["filters"].toArray()[1].toObject()["value"].toDouble(),2.5);
        QVERIFY(result["filters"].toArray()[2].toObject()["value"].isBool());QVERIFY(!result["filters"].toArray()[2].toObject()["value"].toBool());QVERIFY(result["filters"].toArray()[3].toObject()["value"].isNull());
        QCOMPARE(result["filters"].toArray()[4].toObject()["value"].toString(),QString());QCOMPARE(result["sort"].toArray()[6].toObject()["col"].toInt(),8);
    }
    void multiCriteriaDialogAppliesEditsAndCancelsAnExistingProjection() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("criteria.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QList<QStringList> rows{{"ID","Group","Qty","Flag"},{"b","X","2","TRUE"},{"a","X","3","TRUE"},{"c","Y","4","FALSE"},{"d","X","1","TRUE"}};
        QJsonArray cells;for(int row=0;row<rows.size();++row)for(int col=0;col<4;++col)cells.append(QJsonObject{{"row",row+1},{"col",col+1},{"source",rows[row][col]}});
        window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(4,3)),QItemSelectionModel::ClearAndSelect);
        QTimer answer;answer.setInterval(10);bool filled=false;
        connect(&answer,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());dialog && dialog->objectName()=="listCriteriaDialog" && !filled){
            filled=true;auto *sort=dialog->findChild<QTableWidget *>("sortCriteria"),*filters=dialog->findChild<QTableWidget *>("filterCriteria");
            for(int i=0;i<3;++i){dialog->findChild<QPushButton *>("addSort")->click();dialog->findChild<QPushButton *>("addFilter")->click();}
            for(int i=0;i<3;++i)qobject_cast<QSpinBox *>(sort->cellWidget(i,0))->setValue(QList<int>{2,3,1}[i]);qobject_cast<QComboBox *>(sort->cellWidget(1,1))->setCurrentIndex(1);
            for(int i=0;i<3;++i){qobject_cast<QSpinBox *>(filters->cellWidget(i,0))->setValue(i+2);qobject_cast<QComboBox *>(filters->cellWidget(i,2))->setCurrentIndex(i);}
            qobject_cast<QLineEdit *>(filters->cellWidget(0,3))->setText("X");qobject_cast<QComboBox *>(filters->cellWidget(1,1))->setCurrentText(">");qobject_cast<QLineEdit *>(filters->cellWidget(1,3))->setText("1");qobject_cast<QLineEdit *>(filters->cellWidget(2,3))->setText("TRUE");
            if(qEnvironmentVariableIsSet("DATAHORIZON_CRITERIA_SCREENSHOT"))dialog->grab().save(qEnvironmentVariable("DATAHORIZON_CRITERIA_SCREENSHOT"));
            dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();
        }});answer.start();window.findChild<QAction *>("listCriteriaAction")->trigger();answer.stop();QVERIFY(filled);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(model->projected());QCOMPARE(model->rowCount(),3);QCOMPARE(model->sourceRow(1),2);QCOMPARE(model->sourceRow(2),1);
        auto *summary=window.findChild<QLabel *>("listCriteriaSummary");QVERIFY(summary->isVisible());QVERIFY(summary->text().contains("B↑, C↓, A↑"));QVERIFY(summary->toolTip().contains("Boolean"));QVERIFY(changed.isEmpty());
        if(qEnvironmentVariableIsSet("DATAHORIZON_CRITERIA_VIEW_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_CRITERIA_VIEW_SCREENSHOT"));
        QTimer cancel;cancel.setInterval(10);bool inspected=false;connect(&cancel,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());dialog && dialog->objectName()=="listCriteriaDialog"){
            inspected=dialog->findChild<QTableWidget *>("sortCriteria")->rowCount()==3 && dialog->findChild<QTableWidget *>("filterCriteria")->rowCount()==3;dialog->reject();
        }});cancel.start();window.findChild<QAction *>("listCriteriaAction")->trigger();cancel.stop();QVERIFY(inspected);QCOMPARE(opened.count(),3);QCOMPARE(model->rowCount(),3);
        QTimer edit;edit.setInterval(10);connect(&edit,&QTimer::timeout,&window,[&]{if(auto *dialog=qobject_cast<QDialog *>(QApplication::activeModalWidget());dialog && dialog->objectName()=="listCriteriaDialog"){
            auto *filters=dialog->findChild<QTableWidget *>("filterCriteria");qobject_cast<QLineEdit *>(filters->cellWidget(1,3))->setText("2");dialog->findChild<QDialogButtonBox *>()->button(QDialogButtonBox::Ok)->click();
        }});edit.start();window.findChild<QAction *>("listCriteriaAction")->trigger();edit.stop();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QCOMPARE(model->rowCount(),2);QCOMPARE(model->sourceRow(1),2);QVERIFY(changed.isEmpty());
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",3},{"source","=1/0"}}}}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QVERIFY(!model->projected());QVERIFY(!summary->isVisible());QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("List evaluation blocked"));
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,2)).toString(),QString("3"),5000);
    }
    void dataBoundaryNavigationExtendsFromSequenceAnchorAndProtectsDrafts() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("data-nav.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells;for(int row:{1,2,3,10})cells.append(QJsonObject{{"row",row},{"col",1},{"source",QString::number(row)}});
        cells.append(QJsonObject{{"row",10},{"col",2},{"source","20"}});cells.append(QJsonObject{{"row",10},{"col",3},{"source","30"}});
        window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");auto *source=window.findChild<QLineEdit *>("source");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto action=[&](QString name){return window.findChild<QAction *>(name);};
        grid->setCurrentIndex(model->index(0,0));grid->setFocus();QTest::keyClick(grid,Qt::Key_Down,Qt::ControlModifier);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(2,0),5000);
        action("dataJumpUpAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(0,0),5000);
        action("dataExtendDownAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(2,0),5000);
        action("dataExtendDownAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(9,0),5000);
        action("dataExtendRightAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(9,2),5000);
        QCOMPARE(grid->selectionModel()->selection().first().top(),0);QCOMPARE(grid->selectionModel()->selection().first().left(),0);QCOMPARE(grid->selectionModel()->selection().first().width(),3);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(9,2)),5000);source->setText("retained draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);action("dataJumpDownAction")->trigger();QCOMPARE(grid->currentIndex(),model->index(9,2));QCOMPARE(source->text(),QString("retained draft"));
        QTest::keyClick(source,Qt::Key_Escape);grid->selectRow(2);window.findChild<QAction *>("hideRowsAction")->trigger();grid->setCurrentIndex(model->index(0,0));
        action("dataJumpDownAction")->trigger();QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("data boundary is hidden"),5000);QCOMPARE(grid->currentIndex(),model->index(0,0));
        window.findChild<QPushButton *>("showHiddenButton")->click();
        QCOMPARE(action("dataJumpDownAction")->shortcutContext(),Qt::WidgetShortcut);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);source->setFocus();source->setText("draft words");source->setModified(true);
        bool unexpectedDialog=false;QTimer cancel;cancel.setInterval(10);connect(&cancel,&QTimer::timeout,&window,[&]{if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())){unexpectedDialog=true;if(auto *button=box->button(QMessageBox::Cancel))button->click();}});
        cancel.start();QTest::keyClick(source,Qt::Key_Left,Qt::ControlModifier);QTest::qWait(50);cancel.stop();QVERIFY(!unexpectedDialog);QCOMPARE(source->text(),QString("draft words"));
        QTest::keyClick(source,Qt::Key_Escape);QVERIFY(changed.isEmpty());
        window.showList({{"r1",1},{"r2",10},{"c1",1},{"c2",3},{"header",false}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(!action("dataJumpDownAction")->isEnabled());
    }
    void dataBoundaryNavigationDiscardsMovedSelectionEditorAndConnectionReplies() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("nav-late.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        DelayedSummaryServer server(model->state(),model->sheet());QVERIFY(server.listen(QHostAddress::LocalHost,0));auto *client=window.findChild<RestClient *>();
        client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");int completed=0;
        connect(client->findChild<QNetworkAccessManager *>(),&QNetworkAccessManager::finished,&window,[&](QNetworkReply *reply){if(reply->url().path().endsWith("/navigate"))++completed;});
        auto *down=window.findChild<QAction *>("dataJumpDownAction");down->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.navigationRequests.size(),1,3000);
        grid->setCurrentIndex(model->index(0,1));server.respondNavigation(0,100,1);QTRY_COMPARE_WITH_TIMEOUT(completed,1,3000);QCOMPARE(grid->currentIndex(),model->index(0,1));
        down->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.navigationRequests.size(),2,3000);QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,1)),5000);
        grid->edit(model->index(0,1));QPointer<QLineEdit> editor=grid->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("new editor draft");
        server.respondNavigation(1,100,2);QTRY_COMPARE_WITH_TIMEOUT(completed,2,3000);QCOMPARE(grid->currentIndex(),model->index(0,1));QCOMPARE(editor->text(),QString("new editor draft"));
        QTest::keyClick(editor,Qt::Key_Escape);QTRY_VERIFY_WITH_TIMEOUT(editor.isNull(),3000);
        down->trigger();QTRY_COMPARE_WITH_TIMEOUT(server.navigationRequests.size(),3,3000);
        DelayedSummaryServer replacement(model->state(),model->sheet());QVERIFY(replacement.listen(QHostAddress::LocalHost,0));client->configure("http://127.0.0.1:"+QString::number(replacement.serverPort()),"token");
        down->trigger();QTRY_COMPARE_WITH_TIMEOUT(replacement.navigationRequests.size(),1,3000);replacement.respondNavigation(0,5,2);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(4,1),3000);
        server.respondNavigation(2,9,2);QTest::qWait(50);QCOMPARE(grid->currentIndex(),model->index(4,1));
    }
    void frozenPaneTabAndEditorTabAdvanceAcrossSourceColumns() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("pane-tab.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        window.findChild<QAction *>("freezeTopRowAction")->trigger();window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
        auto *corner=window.findChild<QTableView *>("frozenCorner"),*top=window.findChild<QTableView *>("frozenTopRow"),*left=window.findChild<QTableView *>("frozenFirstColumn");
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);corner->edit(corner->model()->index(0,0));auto *editor=corner->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("=2+3");QTest::keyClick(editor,Qt::Key_Tab);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(0,1),5000);QCOMPARE(window.focusWidget(),top);
        QTest::keyClick(top,Qt::Key_Backtab);QCOMPARE(grid->currentIndex(),model->index(0,0));QCOMPARE(window.focusWidget(),corner);
        QTest::keyClick(corner,Qt::Key_Down);QCOMPARE(grid->currentIndex(),model->index(1,0));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,0)),5000);
        left->edit(left->model()->index(1,0));editor=left->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("left entry");QTest::keyClick(editor,Qt::Key_Tab);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QTRY_COMPARE_WITH_TIMEOUT(grid->currentIndex(),model->index(1,1),5000);QCOMPARE(window.focusWidget(),grid);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,0)).toString(),QString("left entry"),5000);
    }
    void frozenPrefixModelsResizeAndForwardSourceIndices() {
        QStandardItemModel source(5,6);FrozenPrefixModel prefix(&source,2,3);
        QAbstractItemModelTester tester(&prefix,QAbstractItemModelTester::FailureReportingMode::QtTest);
        QCOMPARE(prefix.rowCount(),2);QCOMPARE(prefix.columnCount(),3);
        QVERIFY(prefix.setData(prefix.index(1,2),"value",Qt::EditRole));QCOMPARE(source.data(source.index(1,2)).toString(),QString("value"));
        prefix.setLimits(4,2);QCOMPARE(prefix.rowCount(),4);QCOMPARE(prefix.columnCount(),2);
        source.removeRows(2,3);QCOMPARE(prefix.rowCount(),2);source.insertColumn(0);QCOMPARE(prefix.columnCount(),2);
        prefix.setLimits(0,0);QCOMPARE(prefix.rowCount(),0);QCOMPARE(prefix.columnCount(),0);
    }
    void freezeSelectedPanesPreservesSizesSelectionsAndReopenWithBounds() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");const auto path=temp.filePath("multi-pane.dhwb");
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
            grid->setRowHeight(1,49);grid->setColumnWidth(1,137);grid->setCurrentIndex(model->index(2,2));
            QSignalSpy changed(&window,&MainWindow::operationFinished);auto *freeze=window.findChild<QAction *>("freezePanesAction");freeze->trigger();
            auto *top=window.findChild<QTableView *>("frozenTopRow"),*left=window.findChild<QTableView *>("frozenFirstColumn"),*corner=window.findChild<QTableView *>("frozenCorner");
            QCOMPARE(top->model()->rowCount(),2);QCOMPARE(left->model()->columnCount(),2);QCOMPARE(corner->model()->rowCount(),2);QCOMPARE(corner->model()->columnCount(),2);
            QVERIFY(grid->isRowHidden(0));QVERIFY(grid->isRowHidden(1));QVERIFY(grid->isColumnHidden(1));QCOMPARE(top->rowHeight(1),49);QCOMPARE(left->columnWidth(1),137);
            auto *address=window.findChild<QLineEdit *>("address");address->setText("A1:D4");QTest::keyClick(address,Qt::Key_Return);
            QCOMPARE(grid->selectionModel()->selection().first().height(),4);QCOMPARE(corner->selectionModel()->selection().first().height(),2);
            address->setText("B2");QTest::keyClick(address,Qt::Key_Return);QCOMPARE(window.focusWidget(),corner);
            QTest::keyClick(corner,Qt::Key_Down,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(2,1));QCOMPARE(window.focusWidget(),left);
            QTest::keyClick(left,Qt::Key_Right,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(2,2));QCOMPARE(window.focusWidget(),grid);
            grid->setCurrentIndex(model->index(17,9));freeze->trigger();QCOMPARE(top->model()->rowCount(),2);QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("16 rows and 8 columns"));
            grid->selectRow(1);window.findChild<QAction *>("hideRowsAction")->trigger();QVERIFY(!top->isVisible());QVERIFY(left->isVisible());
            window.findChild<QPushButton *>("showHiddenButton")->click();QVERIFY(top->isVisible());
            grid->setCurrentIndex(model->index(2,2));QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(2,2)),5000);
            auto *source=window.findChild<QLineEdit *>("source");source->setText("draft");source->setModified(true);
            answerNextDialog(QMessageBox::Cancel);window.findChild<QAction *>("unfreezePanesAction")->trigger();QVERIFY(corner->isVisible());QCOMPARE(source->text(),QString("draft"));
            QTest::keyClick(source,Qt::Key_Escape);QVERIFY(changed.isEmpty());
            window.findChild<QAction *>("unfreezePanesAction")->trigger();QVERIFY(!corner->isVisible());
            grid->setColumnWidth(0,2000);grid->setCurrentIndex(model->index(0,1));freeze->trigger();QVERIFY(!left->isVisible());
            QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("too little scrolling space"));
            grid->setColumnWidth(0,110);grid->setCurrentIndex(model->index(2,2));freeze->trigger();QVERIFY(corner->isVisible());QVERIFY(changed.isEmpty());
            if(qEnvironmentVariableIsSet("DATAHORIZON_PANES_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_PANES_SCREENSHOT"));
        }
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid"),*corner=window.findChild<QTableView *>("frozenCorner");
            QVERIFY(corner->isVisible());QCOMPARE(corner->model()->rowCount(),2);QCOMPARE(corner->model()->columnCount(),2);
            QCOMPARE(corner->rowHeight(1),49);QCOMPARE(corner->columnWidth(1),137);
            window.findChild<QAction *>("unfreezePanesAction")->trigger();QVERIFY(!corner->isVisible());QVERIFY(!grid->isRowHidden(1));QVERIFY(!grid->isColumnHidden(1));
            QCOMPARE(grid->rowHeight(1),49);QCOMPARE(grid->columnWidth(1),137);
        }
        QSettings settings(options.viewStatePath,QSettings::IniFormat);QVERIFY(!settings.allKeys().isEmpty());
        const auto key=settings.allKeys().first();auto legacy=QJsonDocument::fromJson(settings.value(key).toByteArray()).object();
        legacy["frozen_rows"]=1000000000;legacy["frozen_columns"]="bad";legacy["freeze_top_row"]=true;legacy["freeze_first_column"]=true;
        settings.setValue(key,QJsonDocument(legacy).toJson(QJsonDocument::Compact));settings.sync();
        MainWindow legacyWindow(options);legacyWindow.show();QSignalSpy ready(&legacyWindow,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&legacyWindow,&MainWindow::workbookReady);legacyWindow.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *corner=legacyWindow.findChild<QTableView *>("frozenCorner");QVERIFY(corner->isVisible());QCOMPARE(corner->model()->rowCount(),1);QCOMPARE(corner->model()->columnCount(),1);
    }
    void frozenFirstColumnWrapUsesBoundedSharedRowMeasurement() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("frozen-wrap.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const QString text="A long label in the frozen first column should wrap and share its row height with the scrolling body";
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source",text},{"kind","text"},{"style",QJsonObject{{"wrap",true}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
        auto *left=window.findChild<QTableView *>("frozenFirstColumn"),*grid=window.findChild<QTableView *>("grid");
        left->setColumnWidth(0,90);QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(1)>26,5000);QCOMPARE(left->rowHeight(1),grid->rowHeight(1));
        const int narrow=grid->rowHeight(1);left->setColumnWidth(0,500);QTRY_VERIFY_WITH_TIMEOUT(grid->rowHeight(1)<narrow,5000);
        QCOMPARE(left->rowHeight(1),grid->rowHeight(1));QCOMPARE(window.model()->data(window.model()->index(1,0),Qt::EditRole).toString(),text);
    }
    void frozenFirstColumnAlignsEditsSelectsAndPersistsWithCorner() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        const auto path=temp.filePath("first-column.dhwb");
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
            window.findChild<QAction *>("freezeFirstColumnAction")->trigger();
            auto *left=window.findChild<QTableView *>("frozenFirstColumn"),*top=window.findChild<QTableView *>("frozenTopRow"),*corner=window.findChild<QTableView *>("frozenCorner");
            QVERIFY(left->isVisible());QVERIFY(grid->isColumnHidden(0));QVERIFY(!grid->verticalHeader()->isVisible());
            grid->scrollTo(model->index(10000,40),QAbstractItemView::PositionAtTop);QTest::qWait(50);
            grid->setRowHeight(10000,63);QCOMPARE(left->rowHeight(10000),63);
            QCOMPARE(left->viewport()->mapToGlobal(left->visualRect(left->model()->index(10000,0)).topLeft()).y(),grid->viewport()->mapToGlobal(grid->visualRect(model->index(10000,40)).topLeft()).y());
            const int horizontal=grid->horizontalScrollBar()->value();left->setCurrentIndex(left->model()->index(10000,0));
            QCOMPARE(grid->currentIndex(),model->index(10000,0));QCOMPARE(grid->horizontalScrollBar()->value(),horizontal);
            QTest::keyClick(left,Qt::Key_Right,Qt::ShiftModifier);QCOMPARE(grid->selectionModel()->selection().first().width(),2);
            left->setColumnWidth(0,150);QCOMPARE(left->columnWidth(0),150);
            grid->selectColumn(0);QTimer::singleShot(20,[]{if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())){dialog->setIntValue(151);dialog->accept();}});
            window.findChild<QAction *>("columnWidthAction")->trigger();QCOMPARE(left->columnWidth(0),151);
            auto *address=window.findChild<QLineEdit *>("address");address->setText("A1");QTest::keyClick(address,Qt::Key_Return);QCOMPARE(window.focusWidget(),left);
            window.findChild<QAction *>("freezeTopRowAction")->trigger();QVERIFY(top->isVisible());QVERIFY(corner->isVisible());
            address->setText("A1:C3");QTest::keyClick(address,Qt::Key_Return);QCOMPARE(window.focusWidget(),corner);
            QCOMPARE(grid->selectionModel()->selection().first().width(),3);QCOMPARE(grid->selectionModel()->selection().first().height(),3);
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
            corner->edit(corner->model()->index(0,0));auto *editor=corner->findChild<QLineEdit *>();QVERIFY(editor);
            editor->setText("=3+4");QTest::keyClick(editor,Qt::Key_Return);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
            QTRY_COMPARE_WITH_TIMEOUT(left->model()->data(left->model()->index(1,0)).toString(),QString(),5000);
            QTRY_COMPARE_WITH_TIMEOUT(corner->model()->data(corner->model()->index(0,0)).toString(),QString("7"),5000);
            QCOMPARE(grid->currentIndex(),model->index(1,0));QCOMPARE(window.focusWidget(),left);
            address->setText("A1");QTest::keyClick(address,Qt::Key_Return);
            QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
            corner->edit(corner->model()->index(0,0));editor=corner->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("draft");
            QPointer<QLineEdit> cancelledEditor=editor;
            answerNextDialog(QMessageBox::Cancel);window.findChild<QAction *>("freezeFirstColumnAction")->trigger();QVERIFY(corner->isVisible());
            QTRY_VERIFY_WITH_TIMEOUT(cancelledEditor.isNull(),3000);
            auto *source=window.findChild<QLineEdit *>("source");QCOMPARE(source->text(),QString("draft"));QVERIFY(source->isModified());
            QTest::keyClick(source,Qt::Key_Escape);QCOMPARE(window.focusWidget(),corner);
            corner->edit(corner->model()->index(0,0));editor=corner->findChild<QLineEdit *>();QVERIFY(editor);editor->setText("=4+4");
            window.findChild<QAction *>("saveAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
            QTRY_COMPARE_WITH_TIMEOUT(corner->model()->data(corner->model()->index(0,0)).toString(),QString("8"),5000);
            grid->selectColumn(0);window.findChild<QAction *>("hideColumnsAction")->trigger();QVERIFY(!left->isVisible());QVERIFY(!corner->isVisible());
            window.findChild<QPushButton *>("showHiddenButton")->click();QVERIFY(left->isVisible());QVERIFY(corner->isVisible());
            window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",2},{"header",false}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
            QVERIFY(!left->isVisible());QVERIFY(!corner->isVisible());
            window.findChild<QAction *>("clearListAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);QVERIFY(left->isVisible());
            QVERIFY(model->cacheMetrics()["tiles"].toInt()<=16);
            if(qEnvironmentVariableIsSet("DATAHORIZON_COLUMN_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_COLUMN_SCREENSHOT"));
        }
        {
            MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
            QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(path,false,true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
            auto *left=window.findChild<QTableView *>("frozenFirstColumn"),*corner=window.findChild<QTableView *>("frozenCorner");
            QVERIFY(left->isVisible());QVERIFY(corner->isVisible());QCOMPARE(left->columnWidth(0),151);
            QVERIFY(!(corner->model()->flags(corner->model()->index(0,0))&Qt::ItemIsEditable));
            window.findChild<QAction *>("freezeFirstColumnAction")->trigger();QVERIFY(!left->isVisible());QVERIFY(!window.findChild<QTableView *>("grid")->isColumnHidden(0));
        }
    }
    void frozenPaneSelectionCrossesByKeyboardMouseAndAddressWithoutEdits() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;options.viewStatePath=temp.filePath("views.ini");
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);QSignalSpy opened(&window,&MainWindow::workbookReady);
        window.openBook(temp.filePath("cross-pane.dhwb"),true);QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
        window.findChild<QAction *>("freezeTopRowAction")->trigger();auto *top=window.findChild<QTableView *>("frozenTopRow");
        QSignalSpy changed(&window,&MainWindow::operationFinished);QSignalSpy edits(model,&SheetModel::editRequested);
        auto point=[](QTableView *view,int row,int col){return view->visualRect(view->model()->index(row,col)).center();};
        QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::NoModifier,point(grid,1,1));
        QTest::keyClick(grid,Qt::Key_Up,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(0,1));
        QCOMPARE(grid->selectionModel()->selection().first().height(),2);QCOMPARE(window.focusWidget(),top);
        QTest::keyClick(top,Qt::Key_Down,Qt::ShiftModifier);QCOMPARE(grid->currentIndex(),model->index(1,1));
        QCOMPARE(grid->selectionModel()->selection().first().height(),1);
        auto drag=[&](QTableView *from,int r1,int c1,QTableView *to,int r2,int c2){
            QTest::mousePress(from->viewport(),Qt::LeftButton,Qt::NoModifier,point(from,r1,c1));
            const auto global=to->viewport()->mapToGlobal(point(to,r2,c2));
            QMouseEvent move(QEvent::MouseMove,QPointF(from->viewport()->mapFromGlobal(global)),QPointF(global),Qt::NoButton,Qt::LeftButton,Qt::NoModifier);
            QApplication::sendEvent(from->viewport(),&move);
            QTest::mouseRelease(from->viewport(),Qt::LeftButton,Qt::NoModifier,from->viewport()->mapFromGlobal(global));
        };
        drag(top,0,0,grid,2,2);auto range=grid->selectionModel()->selection().first();
        QCOMPARE(range.top(),0);QCOMPARE(range.left(),0);QCOMPARE(range.bottom(),2);QCOMPARE(range.right(),2);
        QCOMPARE(top->selectionModel()->selection().first().width(),3);
        drag(grid,2,2,top,0,0);QCOMPARE(grid->selectionModel()->selection().first(),range);
        auto *address=window.findChild<QLineEdit *>("address");address->setText("A1:C3");QTest::keyClick(address,Qt::Key_Return);
        QCOMPARE(grid->selectionModel()->selection().first(),range);
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(0,0)),5000);
        auto *source=window.findChild<QLineEdit *>("source");source->setText("draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);QTest::mouseClick(grid->viewport(),Qt::LeftButton,Qt::ShiftModifier,point(grid,3,3));
        QCOMPARE(grid->selectionModel()->selection().first(),range);QCOMPARE(source->text(),QString("draft"));
        QTest::keyClick(source,Qt::Key_Escape);QVERIFY(edits.isEmpty());QVERIFY(changed.isEmpty());
        QAction *bold=nullptr;for(auto *action:window.findChildren<QAction *>())if(action->shortcut().matches(QKeySequence::Bold)==QKeySequence::ExactMatch)bold=action;
        QVERIFY(bold);bold->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(2,2)),5000);QVERIFY(model->cell(model->index(2,2))["style"].toObject()["bold"].toBool());
        QTRY_VERIFY_WITH_TIMEOUT(model->loaded(model->index(0,0)),5000);QVERIFY(model->cell(model->index(0,0))["style"].toObject()["bold"].toBool());
    }
    void formulaAwareCutRetargetsDependentsAndSupportsUndo() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("formula-cut.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}},
            QJsonObject{{"row",1},{"col",2},{"source","=$A$1+1"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("8"),5000);
        QAction *cut=nullptr;for(auto *action:window.findChildren<QAction *>())if(action->shortcut().matches(QKeySequence::Cut)==QKeySequence::ExactMatch)cut=action;
        QVERIFY(cut);grid->setCurrentIndex(model->index(0,0));grid->setFocus();cut->trigger();
        QTRY_VERIFY_WITH_TIMEOUT(QApplication::clipboard()->mimeData()->hasFormat("application/x-datahorizon-cells"),5000);
        grid->setCurrentIndex(model->index(2,0));window.findChild<QAction *>("pasteAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1),Qt::EditRole).toString(),QString("=$A$3+1"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,0)).toString(),QString("7"),5000);
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1),Qt::EditRole).toString(),QString("=$A$1+1"),5000);
    }
    void selectionSummaryReadsWholeStoredRectangleAndHandlesErrorsAndViewGuards() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("summary.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","0.125"},{"style",QJsonObject{{"format","percent2"}}}},
            QJsonObject{{"row",2},{"col",1},{"source","=2+2"}},QJsonObject{{"row",3},{"col",1},{"source","12"},{"kind","text"}},
            QJsonObject{{"row",4},{"col",1},{"source","TRUE"}},QJsonObject{{"row",5},{"col",1},{"source",""},{"kind","text"}},
            QJsonObject{{"row",6},{"col",1},{"source","NULL"},{"kind","null"}},QJsonObject{{"row",1000},{"col",1},{"source","3"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();auto *label=window.findChild<QLabel *>("selectionSummary");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(999,0)),QItemSelectionModel::ClearAndSelect);
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 6 • Sum 7.125 • Average 2.375"),5000);
        QVERIFY(label->toolTip().contains("1000 selected cells; 3 numeric"));QVERIFY(changed.isEmpty());
        window.resize(700,500);QTest::qWait(30);QVERIFY(label->width()<window.width());QCOMPARE(label->accessibleName(),QString("Selection summary"));
        if(qEnvironmentVariableIsSet("DATAHORIZON_SUMMARY_SCREENSHOT"))window.grab().save(qEnvironmentVariable("DATAHORIZON_SUMMARY_SCREENSHOT"));
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(10000,0)),QItemSelectionModel::ClearAndSelect);
        QCOMPARE(label->text(),QString("Summary unavailable"));QVERIFY(label->toolTip().contains("10,000"));
        grid->setCurrentIndex(model->index(1,0));window.findChild<QAction *>("hideRowsAction")->trigger();
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,0)),QItemSelectionModel::ClearAndSelect);
        QVERIFY(label->toolTip().contains("hidden"));window.findChild<QPushButton *>("showHiddenButton")->click();
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 3 • Sum 4.125 • Average 2.0625"),5000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",2},{"col",1},{"source","=1/0"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,0)),QItemSelectionModel::ClearAndSelect);
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 3 • Sum/average unavailable"),5000);
        QVERIFY(label->toolTip().contains("1 formula errors"));
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);QVERIFY(label->toolTip().contains("list view"));
    }
    void selectionSummaryCoalescesAndDiscardsSupersededRepliesAndConnections() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("summary-late.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");auto *label=window.findChild<QLabel *>("selectionSummary");
        DelayedSummaryServer server(model->state(),model->sheet());QVERIFY(server.listen(QHostAddress::LocalHost,0));
        auto *client=window.findChild<RestClient *>();client->configure("http://127.0.0.1:"+QString::number(server.serverPort()),"token");
        grid->setCurrentIndex(model->index(0,0));
        QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),1,3000);
        for(int col=1;col<=8;++col)grid->setCurrentIndex(model->index(0,col));
        QTest::qWait(350);QCOMPARE(server.requests.size(),1);QCOMPARE(label->text(),QString("Summary…"));
        server.respond(0,100);QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),2,3000);
        QCOMPARE(server.requests[1].range["c1"].toInt(),9);QCOMPARE(label->text(),QString("Summary…"));
        server.respond(1,9);QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 1 • Sum 9 • Average 9"),3000);
        grid->setCurrentIndex(model->index(0,9));QTRY_COMPARE_WITH_TIMEOUT(server.requests.size(),3,3000);
        DelayedSummaryServer replacement(model->state(),model->sheet());QVERIFY(replacement.listen(QHostAddress::LocalHost,0));
        client->configure("http://127.0.0.1:"+QString::number(replacement.serverPort()),"token");
        QTRY_COMPARE_WITH_TIMEOUT(replacement.requests.size(),1,3000);replacement.respond(0,10);
        QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Count 1 • Sum 10 • Average 10"),3000);
        server.respond(2,200);QTest::qWait(50);QCOMPARE(label->text(),QString("Count 1 • Sum 10 • Average 10"));
        const auto issues=window.findChild<QPlainTextEdit *>()->toPlainText();
        grid->setCurrentIndex(model->index(0,10));QTRY_COMPARE_WITH_TIMEOUT(replacement.requests.size(),2,3000);
        replacement.respondInvalid(1);QTRY_COMPARE_WITH_TIMEOUT(label->text(),QString("Summary unavailable"),3000);
        QVERIFY(label->toolTip().contains("Invalid selection summary response"));QCOMPARE(window.findChild<QPlainTextEdit *>()->toPlainText(),issues);
    }
    void scopedFindNavigatesWrapsAndInvalidatesAfterEdits() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("find.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","hit"}},
            QJsonObject{{"row",3},{"col",2},{"source","hit"}},QJsonObject{{"row",8},{"col",1},{"source","hit"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
        auto *next=window.findChild<QAction *>("findNextAction"),*previous=window.findChild<QAction *>("findPreviousAction");
        QVERIFY(!next->isEnabled());
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(4,1)),QItemSelectionModel::ClearAndSelect);
        auto search=[&](const QString &needle){
            QTimer answer;answer.setInterval(10);connect(&answer,&QTimer::timeout,&window,[&]{
                if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                    dialog->setTextValue(dialog->windowTitle()=="Search field"?dialog->comboBoxItems().first():needle);dialog->accept();
                }
            });answer.start();window.findChild<QAction *>("findAction")->trigger();
        };
        QSignalSpy changed(&window,&MainWindow::operationFinished);search("hit");
        QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),5000);QCOMPARE(grid->currentIndex(),model->index(0,0));
        next->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        next->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        previous->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        QVERIFY(window.findChild<QLabel *>("storageStatus")->text().contains("2 of 2"));
        QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(2,1)),5000);
        auto *source=window.findChild<QLineEdit *>("source");source->setText("draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);next->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Discard);next->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        previous->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        window.findChild<QAction *>("hideRowsAction")->trigger();
        previous->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        next->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("next match is hidden"));
        window.findChild<QPushButton *>("showHiddenButton")->click();next->trigger();QCOMPARE(grid->currentIndex(),model->index(2,1));
        QVERIFY(changed.isEmpty());
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",3},{"col",2},{"source","changed"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(!next->isEnabled());QVERIFY(!previous->isEnabled());
        search("absent");QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("0 matching cells"),5000);
        QVERIFY(!next->isEnabled());
    }
    void scopedFindTruncationWrapsOnlyTheReportedMatchesAndSupportsDisplay() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("find-limit.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells;for(int r=1;r<=501;++r)cells.append(QJsonObject{{"row",r},{"col",1},{"source","=1/8"},{"style",QJsonObject{{"format","percent2"}}}});
        window.submit({{"action","edit"},{"cells",cells}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(500,0)),QItemSelectionModel::ClearAndSelect);
        QTimer answer;answer.setInterval(10);connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *dialog=qobject_cast<QInputDialog *>(QApplication::activeModalWidget())) {
                dialog->setTextValue(dialog->windowTitle()=="Search field"?dialog->comboBoxItems().at(1):QString("12.50%"));dialog->accept();
            }
        });answer.start();window.findChild<QAction *>("findAction")->trigger();answer.stop();
        auto *next=window.findChild<QAction *>("findNextAction");QTRY_VERIFY_WITH_TIMEOUT(next->isEnabled(),5000);
        QVERIFY(window.findChild<QLabel *>("storageStatus")->text().contains("first 500 / 501"));
        window.findChild<QAction *>("findPreviousAction")->trigger();QCOMPARE(grid->currentIndex(),model->index(499,0));
        next->trigger();QCOMPARE(grid->currentIndex(),model->index(0,0));
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);QVERIFY(!next->isEnabled());
    }
    void addressRangesNormalizeBoundsAndProtectDraftsAndViewGuards() {
        QTemporaryDir temp;StartupOptions options;options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("ranges.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *grid=window.findChild<QTableView *>("grid");auto *model=window.model();
        auto *address=window.findChild<QLineEdit *>("address");auto *source=window.findChild<QLineEdit *>("source");
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        auto go=[&](QString value){address->setText(value);QTest::keyClick(address,Qt::Key_Return);};
        go(" $d$5:$b$2 ");QCOMPARE(grid->currentIndex(),model->index(4,3));
        auto rect=grid->selectionModel()->selection().first();QCOMPARE(rect.top(),1);QCOMPARE(rect.left(),1);QCOMPARE(rect.bottom(),4);QCOMPARE(rect.right(),3);
        const auto saved=grid->selectionModel()->selection();
        for(const auto &bad:QStringList{"A0","XFE1","A1048577","A1:XFD1048576","Sheet1!A1","A1,B2"}) {go(bad);QCOMPARE(grid->selectionModel()->selection(),saved);}
        go("XFD1048576");QCOMPARE(grid->currentIndex(),model->index(1048575,16383));
        go("B2:C3");QTRY_VERIFY_WITH_TIMEOUT(model->sourceReady(model->index(1,1)),5000);
        source->setFocus();source->setText("draft");source->setModified(true);
        answerNextDialog(QMessageBox::Cancel);go("E5:F6");QCOMPARE(grid->currentIndex(),model->index(1,1));QCOMPARE(source->text(),QString("draft"));
        answerNextDialog(QMessageBox::Discard);go("E5:F6");QCOMPARE(grid->currentIndex(),model->index(4,4));
        go("B2");window.findChild<QAction *>("hideRowsAction")->trigger();
        const auto visible=grid->currentIndex();go("A1:B3");QCOMPARE(grid->currentIndex(),visible);
        window.findChild<QPushButton *>("showHiddenButton")->click();
        window.findChild<QAction *>("freezeTopRowAction")->trigger();
        go("C1");QCOMPARE(grid->currentIndex(),model->index(0,2));
        QCOMPARE(window.focusWidget(),window.findChild<QTableView *>("frozenTopRow"));
        go("A1:B2");QCOMPARE(grid->currentIndex(),model->index(0,0));
        QCOMPARE(grid->selectionModel()->selection().first().bottom(),1);
        window.findChild<QAction *>("freezeTopRowAction")->trigger();
        window.showList({{"r1",1},{"r2",3},{"c1",1},{"c2",1},{"header",false}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const auto projected=grid->currentIndex();go("B2:C3");QCOMPARE(grid->currentIndex(),projected);
        QTest::qWait(50);QVERIFY(changed.isEmpty());
    }
    void directionalFillUsesIndependentSeedsAndUndo() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options);window.show();QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady);window.openBook(temp.filePath("fill.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","1"}},
            QJsonObject{{"row",1},{"col",2},{"source","=A1+1"},{"style",QJsonObject{{"bold",true}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model();auto *grid=window.findChild<QTableView *>("grid");
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,1)),QItemSelectionModel::ClearAndSelect);
        window.findChild<QAction *>("fillDownAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("2"),5000);
        QCOMPARE(model->data(model->index(2,1),Qt::EditRole).toString(),QString("=A3+1"));
        QVERIFY(model->cell(model->index(2,1))["style"].toObject()["bold"].toBool());
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(2,2)),QItemSelectionModel::ClearAndSelect);
        window.findChild<QAction *>("fillRightAction")->trigger();QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,2)).toString(),QString("1"),5000);
        window.submit({{"action","undo"}});QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(2,1)).toString(),QString("2"),5000);
        QCOMPARE(model->data(model->index(2,1),Qt::EditRole).toString(),QString("=A3+1"));
    }
    void clearCommandsDistinguishInputsFormatsAndAllWithUndo() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("clear.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=1/8"},{"style",QJsonObject{{"format","percent2"},{"bold",true}}}},
            QJsonObject{{"row",1},{"col",2},{"source","00123"},{"kind","text"},{"style",QJsonObject{{"background","#12ab34"}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); auto *model=window.model();
        auto select=[&]{grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(0,1)),QItemSelectionModel::ClearAndSelect);};
        select(); window.findChild<QAction *>("clearFormatsAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("0.125"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("00123"),5000);
        QVERIFY(model->cell(model->index(0,0))["style"].toObject().isEmpty());
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12.50%"),5000);
        select(); window.findChild<QAction *>("clearAllAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),5,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString(),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString(),5000);
        QVERIFY(model->cell(model->index(0,0))["style"].toObject().isEmpty());
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),6,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString("12.50%"),5000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("00123"),5000);
        select(); window.findChild<QAction *>("clearContentsAction")->trigger(); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),7,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,0)).toString(),QString(),5000);
        QCOMPARE(model->cell(model->index(0,0))["style"].toObject()["format"].toString(),QString("percent2"));
    }
    void rejectedClearAllRetainsInputsAndHandlerGeneratedCells() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false; options.handler=QString(DATAHORIZON_FIXTURES)+"/validation_hook.py";
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("reject-clear.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"},{"style",QJsonObject{{"bold",true}}}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); auto *grid=window.findChild<QTableView *>("grid");
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(0,1)).toString(),QString("14"),5000);
        grid->selectionModel()->select(QItemSelection(model->index(0,0),model->index(0,1)),QItemSelectionModel::ClearAndSelect);
        QSignalSpy changed(&window,&MainWindow::operationFinished); window.findChild<QAction *>("clearAllAction")->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QVERIFY(!changed[0][0].toBool());
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("7")); QCOMPARE(model->data(model->index(0,1)).toString(),QString("14"));
    }
    void percentageActionPreservesFormulaAndSupportsUndo() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("percentage.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","=1/8"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); grid->setCurrentIndex(window.model()->index(0,0));
        auto *percent=window.findChild<QAction *>("percentFormatAction"); QVERIFY(percent); percent->trigger();
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("12.50%"),5000);
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("0.125"),5000);
    }
    void numericFunctionsRenderDecimalRoundingRules() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("rounding.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells; const QStringList formulas{"=ABS(-7)","=ROUND(-2.5,0)","=ROUNDUP(-1.21,1)","=ROUNDDOWN(-1.29,1)"};
        for(int i=0;i<formulas.size();++i) cells.append(QJsonObject{{"row",1},{"col",i+1},{"source",formulas[i]}});
        window.submit({{"action","edit"},{"cells",cells}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"7","-3","-1.3","-1.2"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i)).toString(),expected[i],5000);
    }
    void iferrorRendersFallbackWithoutHidingCircularReferences() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("iferror.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{
            QJsonObject{{"row",1},{"col",1},{"source","=IFERROR(1/0,42)"}},
            QJsonObject{{"row",1},{"col",2},{"source","=IFERROR(7,1/0)"}},
            QJsonObject{{"row",1},{"col",3},{"source","=IFERROR(C1,0)"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"42","7","#CYCLE!"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i)).toString(),expected[i],5000);
    }
    void logicalFunctionsRenderThroughTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("logical.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells;
        const QStringList formulas{"=AND(TRUE,1)","=OR(FALSE,0)","=NOT(FALSE)","=AND(FALSE,1/0)"};
        for(int i=0;i<formulas.size();++i) cells.append(QJsonObject{{"row",1},{"col",i+1},{"source",formulas[i]}});
        window.submit({{"action","edit"},{"cells",cells}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"TRUE","FALSE","TRUE","#DIV/0!"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i)).toString(),expected[i],5000);
    }
    void aggregateFunctionsRenderThroughTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("functions.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QJsonArray cells{QJsonObject{{"row",1},{"col",1},{"source","2"}},QJsonObject{{"row",2},{"col",1},{"source","4"}}};
        const QStringList functions{"AVERAGE","MIN","MAX","COUNT","COUNTA"};
        for(int i=0;i<functions.size();++i) cells.append(QJsonObject{{"row",1},{"col",i+2},{"source","="+functions[i]+"(A1:A2)"}});
        window.submit({{"action","edit"},{"cells",cells}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        const QStringList expected{"3","2","4","2","2"};
        for(int i=0;i<expected.size();++i) QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,i+1)).toString(),expected[i],5000);
    }
    void installedRuntimeResolutionNeverFallsBackToDevelopmentSources() {
        QTemporaryDir temp; const auto bin=temp.filePath("bin"); QVERIFY(QDir().mkpath(bin));
        auto missing=resolveServicePaths(bin); QVERIFY(!missing.error.isEmpty()); QVERIFY(!missing.developmentFallback);
        const auto service=temp.filePath("share/datahorizon/service/datahorizon"); QVERIFY(QDir().mkpath(service));
        QFile launcher(service+"/launch.py"); QVERIFY(launcher.open(QIODevice::WriteOnly)); launcher.write("# fixture"); launcher.close();
        const auto selected=resolveServicePaths(bin,"/trusted/python"); QCOMPARE(selected.python,QString("/trusted/python")); QCOMPARE(selected.runtimeSource,QString("override"));
        QVERIFY(selected.error.isEmpty()); QVERIFY(!selected.developmentFallback);
        const auto runtime=temp.filePath("share/datahorizon/runtime"); QVERIFY(QDir().mkpath(runtime));
        const auto incomplete=resolveServicePaths(bin); QCOMPARE(incomplete.runtimeSource,QString("bundled")); QVERIFY(!incomplete.error.isEmpty());
        const auto overrideIncomplete=resolveServicePaths(bin,"/trusted/python"); QVERIFY(overrideIncomplete.error.isEmpty());
    }
    void interruptedAtomicExportRetainsOldOrCommittedDestination() {
        QTemporaryDir temp; const auto path=temp.filePath("export.xlsx");
        QFile original(path); QVERIFY(original.open(QIODevice::WriteOnly)); original.write("retained"); original.close();
        auto helper=QCoreApplication::applicationDirPath()+"/savefile_crash";
#ifdef Q_OS_WIN
        helper+=".exe";
#endif
        for(const auto &phase:{QString("before"),QString("after")}) {
            QProcess process; process.start(helper,{path,phase}); QVERIFY(process.waitForStarted()); QVERIFY(process.waitForFinished(10000));
            QCOMPARE(process.exitCode(),73);
            QFile result(path); QVERIFY(result.open(QIODevice::ReadOnly));
            QCOMPARE(result.readAll(),phase=="before"?QByteArray("retained"):QByteArray("replacement"));
        }
    }
    void nativeInspectionPreservesTheOpenWorkbookAndEditorDraft() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto path=temp.filePath("inspect.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(path,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto sheet=window.model()->sheet();
        auto *source=window.findChild<QLineEdit *>("source"); QVERIFY(source);
        source->setText("retained draft"); source->setModified(true);
        bool inspected=false; QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                if(box->objectName()=="nativeInspectionReview") { inspected=true; box->button(QMessageBox::Ok)->click(); }
            }
        });
        QTimer::singleShot(4000,&window,[]{
            for(auto *widget:QApplication::topLevelWidgets()) if(auto *box=qobject_cast<QMessageBox *>(widget)) {
                qWarning()<<"Inspection fixture unexpected modal"<<box->objectName()<<box->text();
                if(auto *button=box->button(QMessageBox::Ok)) button->click(); else box->reject();
            }
        });
        answer.start(); window.inspectNativeFile(path);
        QTRY_VERIFY_WITH_TIMEOUT(inspected,5000); answer.stop();
        QCOMPARE(opened.count(),1); QCOMPARE(window.model()->sheet(),sheet);
        QCOMPARE(source->text(),QString("retained draft")); QVERIFY(source->isModified());
        source->setModified(false);
    }
    void spreadsheetExportFailuresPreserveExistingFilesAndNativeAliases() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        const auto native=temp.filePath("native.dhwb");
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(native,true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QFile source(native); QVERIFY(source.open(QIODevice::ReadOnly)); const auto original=source.readAll(); source.close();
        const auto alias=temp.filePath("native-alias.xlsx"); QVERIFY(QFile::link(native,alias));
        auto *issues=window.findChild<QPlainTextEdit *>(); QVERIFY(issues);
        for(const auto &format:{QString("xlsx"),QString("ods")}) {
            window.exportSpreadsheetTo(alias,format); QVERIFY(issues->toPlainText().contains("native workbook"));
            const auto existing=temp.filePath("retained."+format); QFile file(existing); QVERIFY(file.open(QIODevice::WriteOnly)); file.write("retained"); file.close();
            bool reviewed=false; QTimer answer; answer.setInterval(10);
            connect(&answer,&QTimer::timeout,&window,[&]{
                if(auto *box=qobject_cast<QMessageBox *>(QApplication::activeModalWidget())) {
                    if(box->objectName()=="spreadsheetExportReview") { reviewed=true; box->button(QMessageBox::Cancel)->click(); }
                }
            });
            QTimer::singleShot(5000,&answer,[]{
                for(auto *widget:QApplication::topLevelWidgets()) if(auto *box=qobject_cast<QMessageBox *>(widget)) {
                    if(auto *button=box->button(QMessageBox::Cancel)) button->click(); else if(auto *button=box->button(QMessageBox::Ok)) button->click();
                }
            });
            answer.start(); window.exportSpreadsheetTo(existing,format);
            QTRY_VERIFY_WITH_TIMEOUT(reviewed,5000); answer.stop();
            QVERIFY(file.open(QIODevice::ReadOnly)); QCOMPARE(file.readAll(),QByteArray("retained")); file.close();
            const auto directory=temp.filePath("directory-"+format); QVERIFY(QDir().mkdir(directory));
            answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(directory,format);
            QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Spreadsheet destination failed"),5000);
            issues->clear();
        }
        QVERIFY(source.open(QIODevice::ReadOnly)); QCOMPARE(source.readAll(),original);
    }
    void odsImportAndExportPreserveFormattedFormulaThroughTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("ods.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QFile fixture(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.ods")); QVERIFY(fixture.open(QIODevice::ReadOnly));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Yes); window.importSpreadsheetBytes(fixture.readAll(),"ods");
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        QVERIFY(window.model()->data(window.model()->index(0,1),Qt::FontRole).value<QFont>().bold());
        auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid);
        grid->selectionModel()->select(QItemSelection(window.model()->index(0,0),window.model()->index(0,2)),QItemSelectionModel::ClearAndSelect);
        const auto output=temp.filePath("copy.ods");
        answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(output,"ods");
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(output),5000);
        QFile exported(output); QVERIFY(exported.open(QIODevice::ReadOnly)); QVERIFY(exported.readAll().startsWith("PK"));
    }
    void xlsxImportReviewAndExportUseTheManagedGrid() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("xlsx.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QFile fixture(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.xlsx")); QVERIFY(fixture.open(QIODevice::ReadOnly));
        const auto data=fixture.readAll();
        answerNextDialog(QMessageBox::Cancel); window.importSpreadsheetBytes(data);
        auto *status=window.findChild<QLabel *>("status"); Q_UNUSED(status);
        QTRY_VERIFY_WITH_TIMEOUT(!window.findChild<QProgressDialog *>("xlsxPreparationProgress"),5000);
        QTRY_VERIFY_WITH_TIMEOUT(!QApplication::activeModalWidget(),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString());
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Yes); window.importSpreadsheetBytes(data);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        QVERIFY(window.model()->data(window.model()->index(0,1),Qt::FontRole).value<QFont>().bold());
        if(qEnvironmentVariableIsSet("DATAHORIZON_XLSX_SCREENSHOT")) window.grab().save(qEnvironmentVariable("DATAHORIZON_XLSX_SCREENSHOT"));
        auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid);
        grid->selectionModel()->select(QItemSelection(window.model()->index(0,0),window.model()->index(0,2)),QItemSelectionModel::ClearAndSelect);
        const auto output=temp.filePath("copy.xlsx");
        answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(output);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(output),5000);
        QFile exported(output); QVERIFY(exported.open(QIODevice::ReadOnly)); QVERIFY(exported.readAll().startsWith("PK"));
    }
    void wholeWorkbookImportReplacesEmptyCatalogAndExportsSnapshot_data() {
        QTest::addColumn<QString>("format");
        QTest::newRow("xlsx")<<QString("xlsx"); QTest::newRow("ods")<<QString("ods");
    }
    void wholeWorkbookImportReplacesEmptyCatalogAndExportsSnapshot() {
        QFETCH(QString,format);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("whole.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QFile fixture(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.")+format); QVERIFY(fixture.open(QIODevice::ReadOnly));
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answerNextDialog(QMessageBox::Yes); window.importSpreadsheetBytes(fixture.readAll(),format,"formulas",0);
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString("14.00"),5000);
        // The public export helper captures the full reviewed context itself.
        const auto output=temp.filePath("whole."+format);
        answerNextDialog(QMessageBox::Yes); window.exportSpreadsheetTo(output,format);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(output),5000);
    }
    void cancellingCsvPreparationCannotOpenReviewOrWriteCells() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("cancel.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        window.importCsvText("must not be written");
        auto *progress=window.findChild<QProgressDialog *>("csvPreparationProgress"); QVERIFY(progress);
        auto *cancel=progress->findChild<QPushButton *>(); QVERIFY(cancel); cancel->click();
        auto *issues=window.findChild<QPlainTextEdit *>(); QVERIFY(issues);
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("preparation cancelled"),5000);
        QCOMPARE(changed.count(),0); QVERIFY(!qobject_cast<QMessageBox *>(QApplication::activeModalWidget()));
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString());
    }
    void unifiedImportDetectsExtensions_data() {
        QTest::addColumn<QString>("extension");
        QTest::addColumn<QString>("expectedFirst");
        QTest::addColumn<QString>("expectedSecond");
        QTest::newRow("tsv")<<QString("TSV")<<QString("00123")<<QString("7");
        QTest::newRow("text")<<QString("txt")<<QString("00123")<<QString("7");
        QTest::newRow("xlsx")<<QString("XLSX")<<QString("7")<<QString("14.00");
        QTest::newRow("ods")<<QString("ods")<<QString("7")<<QString("14.00");
    }
    void unifiedImportDetectsExtensions() {
        QFETCH(QString,extension); QFETCH(QString,expectedFirst); QFETCH(QString,expectedSecond);
        const bool nativeDialogs=QApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,true);
        const auto restore=qScopeGuard([nativeDialogs]{QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,nativeDialogs);});
        QTemporaryDir temp; const auto path=temp.filePath("input."+extension);
        const bool tabular=extension=="TSV" || extension=="txt";
        if(tabular) { QFile file(path); QVERIFY(file.open(QIODevice::WriteOnly)); file.write(extension=="TSV"?"00123\t7":"00123,7"); }
        else QVERIFY(QFile::copy(QStringLiteral(DATAHORIZON_FIXTURES "/inputs.")+extension.toLower(),path));
        StartupOptions options; options.automaticWorkbook=false; MainWindow window(options); window.show();
        QSignalSpy ready(&window,&MainWindow::serviceReady); QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("native.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        auto *import=window.findChild<QAction *>("importAction"); QVERIFY(import);
        int imports=0,exports=0;
        auto *fileMenu=qobject_cast<QMenu *>(import->parent());QVERIFY(fileMenu);
        for(auto *action:fileMenu->actions()) {
            imports+=action->text().startsWith("Import"); exports+=action->text().startsWith("Export");
        }
        QCOMPARE(imports,1); QCOMPARE(exports,1);
        bool pickerSeen=false,optionsSeen=false,correctDelimiter=true,timedOut=false;
        QElapsedTimer elapsed; elapsed.start(); QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) { timedOut=true; if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject(); return; }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(!pickerSeen) {
                    pickerSeen=true;
                    QCOMPARE(picker->nameFilters().size(),1);
                    QVERIFY(picker->nameFilters()[0].contains("*.ods")); QVERIFY(!picker->nameFilters()[0].contains("(*)"));
                    picker->setDirectory(temp.path());
                    auto *name=picker->findChild<QLineEdit *>("fileNameEdit"); QVERIFY(name); name->setText(path);
                }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *button=buttons->button(QDialogButtonBox::Open);button && button->isEnabled()) QTimer::singleShot(0,button,[button]{button->click();});
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()==(tabular?"csvOptionsDialog":"spreadsheetOptionsDialog")) {
                optionsSeen=true;
                if(tabular) correctDelimiter=dialog->findChild<QComboBox *>("csvDelimiter")->currentData().toString()==(extension=="TSV"?"\t":",");
                dialog->accept();
            } else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(auto *button=box->button(QMessageBox::Yes)) button->click();
            }
        });
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answer.start(); import->trigger(); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop();
        QVERIFY(!timedOut); QVERIFY(pickerSeen); QVERIFY(optionsSeen); QVERIFY(correctDelimiter); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),expectedFirst,5000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),expectedSecond,5000);
    }
    void unifiedExportUsesTheFormatDropdown_data() {
        QTest::addColumn<int>("formatIndex"); QTest::addColumn<QString>("extension"); QTest::addColumn<bool>("whole");
        QTest::newRow("csv")<<0<<QString("csv")<<false; QTest::newRow("xlsx")<<1<<QString("xlsx")<<false; QTest::newRow("ods")<<2<<QString("ods")<<false;
        QTest::newRow("xlsx-workbook")<<1<<QString("xlsx")<<true; QTest::newRow("ods-workbook")<<2<<QString("ods")<<true;
    }
    void unifiedExportUsesTheFormatDropdown() {
        QFETCH(int,formatIndex); QFETCH(QString,extension); QFETCH(bool,whole);
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("native.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        window.submit({{"action","edit"},{"cells",QJsonArray{QJsonObject{{"row",1},{"col",1},{"source","7"}},QJsonObject{{"row",1},{"col",2},{"source","=A1*2"}}}}});
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *grid=window.findChild<QTableView *>("grid"); QVERIFY(grid);
        grid->selectionModel()->select(QItemSelection(window.model()->index(0,whole?1:0),window.model()->index(0,1)),QItemSelectionModel::ClearAndSelect);
        auto *action=window.findChild<QAction *>("exportAction"); QVERIFY(action);
        bool pickerSeen=false,timedOut=false; QElapsedTimer elapsed; elapsed.start(); QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) { timedOut=true; if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject(); return; }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(!pickerSeen) {
                    pickerSeen=true; QCOMPARE(picker->nameFilters().size(),3);
                    picker->setDirectory(temp.path());
                    auto *name=picker->findChild<QLineEdit *>("fileNameEdit"); QVERIFY(name); name->setText(temp.filePath("result.csv"));
                    auto *formats=picker->findChild<QComboBox *>("fileTypeCombo"); QVERIFY(formats);
                    formats->setFocus(); QTest::keyClick(formats,Qt::Key_Home);
                    for(int i=0;i<formatIndex;++i) QTest::keyClick(formats,Qt::Key_Down);
                    QCOMPARE(picker->selectedNameFilter(),picker->nameFilters()[formatIndex]);
                    if(qEnvironmentVariableIsSet("DATAHORIZON_EXPORT_DIALOG_SCREENSHOT")) picker->grab().save(qEnvironmentVariable("DATAHORIZON_EXPORT_DIALOG_SCREENSHOT"));
                }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *button=buttons->button(QDialogButtonBox::Save);button && button->isEnabled()) QTimer::singleShot(0,button,[button]{button->click();});
            } else if(auto *input=qobject_cast<QInputDialog *>(modal)) {
                if(whole) input->setTextValue(input->comboBoxItems().value(2));
                input->accept();
            }
            else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(auto *button=box->button(QMessageBox::Yes)) button->click();
            }
        });
        answer.start(); action->trigger();
        const auto path=temp.filePath("result."+extension);
        QTRY_VERIFY_WITH_TIMEOUT(QFile::exists(path),10000); answer.stop(); QVERIFY(pickerSeen); QVERIFY(!timedOut);
        QFile output(path); QVERIFY(output.open(QIODevice::ReadOnly)); const auto data=output.readAll();
        if(extension=="csv") QCOMPARE(data,QByteArray("7,14\r\n"));
        else if(extension=="xlsx") { QVERIFY(data.startsWith("PK")); QVERIFY(data.contains("xl/workbook.xml")); }
        else { QVERIFY(data.startsWith("PK")); QVERIFY(data.contains("application/vnd.oasis.opendocument.spreadsheet")); }
        QCOMPARE(opened.count(),2); QVERIFY(extension=="csv" || !QFile::exists(temp.filePath("result.csv")));
    }
    void unifiedExportConfirmsTheNormalizedFilenameBeforeOverwriting() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("native.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        const auto path=temp.filePath("retained.xlsx"); QFile original(path); QVERIFY(original.open(QIODevice::WriteOnly)); original.write("retained"); original.close();
        bool pickerSeen=false,declined=false,confirmedExactPath=false,timedOut=false;
        QElapsedTimer elapsed; elapsed.start(); QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) {
                timedOut=true; if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject(); return;
            }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(declined || elapsed.elapsed()>10000) { timedOut=elapsed.elapsed()>10000; picker->reject(); return; }
                if(!pickerSeen) {
                    pickerSeen=true; picker->setDirectory(temp.path()); picker->selectNameFilter(picker->nameFilters()[1]);
                    // A conflicting suffix typed after choosing the format must
                    // still be normalized before the overwrite question.
                    auto *name=picker->findChild<QLineEdit *>("fileNameEdit"); QVERIFY(name); name->setText("retained.csv");
                }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *button=buttons->button(QDialogButtonBox::Save);button && button->isEnabled()) QTimer::singleShot(0,button,[button]{button->click();});
            } else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                confirmedExactPath=box->text().contains("retained.xlsx"); declined=true;
                if(auto *button=box->button(QMessageBox::No)) button->click();
                else if(auto *button=box->button(QMessageBox::Cancel)) button->click();
            }
        });
        auto *action=window.findChild<QAction *>("exportAction"); QVERIFY(action);
        answer.start(); action->trigger(); answer.stop();
        QVERIFY(!timedOut); QVERIFY(pickerSeen); QVERIFY(declined); QVERIFY(confirmedExactPath);
        QVERIFY(original.open(QIODevice::ReadOnly)); QCOMPARE(original.readAll(),QByteArray("retained"));
        QVERIFY(!QFile::exists(temp.filePath("retained.csv")));
    }
    void csvEncodingAndDelimiterOptionsDecodeBeforeReviewAndRejectInvalidBytes() {
        const bool originalDialogs=QApplication::testAttribute(Qt::AA_DontUseNativeDialogs);
        QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,true);
        const auto restore=qScopeGuard([originalDialogs]{QApplication::setAttribute(Qt::AA_DontUseNativeDialogs,originalDialogs);});
        QTemporaryDir temp; const auto path=temp.filePath("data.csv"); QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly)); file.write(QByteArray::fromHex("30303132333b636166e93b3d312b31")); file.close();
        StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("csv.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        bool selectedOptions=false,reviewed=false,pickerInitialized=false,timedOut=false;
        QElapsedTimer elapsed; elapsed.start();
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(elapsed.elapsed()>10000) {
                timedOut=true;
                if(auto *dialog=qobject_cast<QDialog *>(modal)) dialog->reject();
                return;
            }
            if(auto *picker=qobject_cast<QFileDialog *>(modal)) {
                if(!pickerInitialized) {
                    pickerInitialized=true; picker->setDirectory(temp.path()); picker->selectFile(QFileInfo(path).fileName());
                }
                if(auto *buttons=picker->findChild<QDialogButtonBox *>())
                    if(auto *open=buttons->button(QDialogButtonBox::Open);open && open->isEnabled()) open->click();
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvOptionsDialog") {
                auto *encoding=dialog->findChild<QComboBox *>("csvEncoding");
                auto *delimiter=dialog->findChild<QComboBox *>("csvDelimiter");
                encoding->setCurrentIndex(encoding->findData("cp1252")); delimiter->setCurrentIndex(delimiter->findData(";"));
                selectedOptions=true; dialog->accept();
            } else if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                const auto review=box->text()+box->informativeText()+box->detailedText();
                reviewed=box->textFormat()==Qt::PlainText && review.contains("cp1252") && review.contains(QString::fromUtf8("café"));
                box->button(QMessageBox::Yes)->click();
            }
        });
        QAction *import=nullptr; for(auto *action:window.findChildren<QAction *>()) if(action->objectName()=="importAction") import=action;
        QVERIFY(import); QSignalSpy changed(&window,&MainWindow::operationFinished);
        answer.start(); import->trigger(); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop();
        QVERIFY(!timedOut); QVERIFY(selectedOptions); QVERIFY(reviewed); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,1)).toString(),QString::fromUtf8("café"),5000);
        QCOMPARE(window.model()->data(window.model()->index(0,0)).toString(),QString("00123"));
        QCOMPARE(window.model()->data(window.model()->index(0,2)).toString(),QString("=1+1"));
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        window.importCsvBytes(QByteArray::fromHex("ff"));
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("invalid for utf-8"),5000);
        QCOMPARE(changed.count(),1);
    }
    void csvColumnTypesReviewConvertsHeadersNumbersBooleansAndDatesAtomically() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("types.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        int phase=0; bool reviewed=false; QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(phase==0) { phase=1; box->findChild<QPushButton *>("csvTypesButton")->click(); }
                else if(phase==2) {
                    reviewed=box->informativeText().contains("1 ambiguous") && box->informativeText().contains("2026-03-04");
                    const auto screenshot=qEnvironmentVariable("DATAHORIZON_CSV_SCREENSHOT");
                    if(!screenshot.isEmpty()) box->grab().save(screenshot);
                    phase=3; box->button(QMessageBox::Yes)->click();
                }
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvTypesDialog" && phase==1) {
                dialog->findChild<QCheckBox *>("csvHeader")->setChecked(true);
                auto *column=dialog->findChild<QSpinBox *>("csvTypeColumn"); auto *type=dialog->findChild<QComboBox *>("csvColumnType");
                column->setValue(2); type->setCurrentIndex(type->findData("number"));
                column->setValue(3); type->setCurrentIndex(type->findData("boolean"));
                column->setValue(4); type->setCurrentIndex(type->findData("date_mdy"));
                phase=2; dialog->accept();
            }
        });
        QSignalSpy changed(&window,&MainWindow::operationFinished);
        answer.start(); window.importCsvText("ID,Qty,Active,When,Formula\n00123,2,TRUE,03/04/2026,=1+1");
        QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop(); QVERIFY(reviewed); QVERIFY(changed[0][0].toBool());
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        auto *model=window.model(); QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString("2026-03-04"),5000);
        QCOMPARE(model->cell(model->index(0,1))["input_kind"].toString(),QString("text"));
        QCOMPARE(model->cell(model->index(1,1))["input_kind"].toString(),QString("number"));
        QCOMPARE(model->cell(model->index(1,2))["value"].toBool(),true);
        QCOMPARE(model->data(model->index(1,0)).toString(),QString("00123"));
        QCOMPARE(model->data(model->index(1,4)).toString(),QString("=1+1"));
        window.submit({{"action","undo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString(),5000);
        window.submit({{"action","redo"}}); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),4,10000);
        QTRY_COMPARE_WITH_TIMEOUT(model->data(model->index(1,3)).toString(),QString("2026-03-04"),5000);
    }
    void csvInvalidConversionDisablesApplyAndCanBeCorrectedToText() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("invalid-types.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished); int phase=0; bool blocked=false;
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(auto *box=qobject_cast<QMessageBox *>(modal)) {
                if(phase==0) {phase=1; box->findChild<QPushButton *>("csvTypesButton")->click();}
                else if(phase==2) {
                    blocked=!box->button(QMessageBox::Yes)->isEnabled() && box->detailedText().contains("INVALID_INPUT") && changed.count()==0;
                    phase=3; box->findChild<QPushButton *>("csvTypesButton")->click();
                } else if(phase==4) {phase=5; box->button(QMessageBox::Yes)->click();}
            } else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvTypesDialog") {
                auto *type=dialog->findChild<QComboBox *>("csvColumnType");
                type->setCurrentIndex(type->findData(phase==1?"number":"text")); ++phase; dialog->accept();
            }
        });
        answer.start(); window.importCsvText("=1+1"); QTRY_COMPARE_WITH_TIMEOUT(changed.count(),1,10000); answer.stop();
        QVERIFY(blocked); QVERIFY(changed[0][0].toBool()); QTRY_COMPARE_WITH_TIMEOUT(opened.count(),2,10000);
        QTRY_COMPARE_WITH_TIMEOUT(window.model()->data(window.model()->index(0,0)).toString(),QString("=1+1"),5000);
        QCOMPARE(window.model()->cell(window.model()->index(0,0))["input_kind"].toString(),QString("text"));
    }
    void csvTypeDialogCannotApplyAfterProjectionChanges() {
        QTemporaryDir temp; StartupOptions options; options.automaticWorkbook=false;
        MainWindow window(options); window.show(); QSignalSpy ready(&window,&MainWindow::serviceReady);
        QTRY_COMPARE_WITH_TIMEOUT(ready.count(),1,10000);
        QSignalSpy opened(&window,&MainWindow::workbookReady); window.openBook(temp.filePath("stale-types.dhwb"),true);
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),1,10000);
        QSignalSpy changed(&window,&MainWindow::operationFinished); bool invalidated=false;
        QTimer answer; answer.setInterval(10);
        connect(&answer,&QTimer::timeout,&window,[&]{
            auto *modal=QApplication::activeModalWidget();
            if(auto *box=qobject_cast<QMessageBox *>(modal)) box->findChild<QPushButton *>("csvTypesButton")->click();
            else if(auto *dialog=qobject_cast<QDialog *>(modal);dialog && dialog->objectName()=="csvTypesDialog") {
                window.model()->setProjection({0,1}); invalidated=true; dialog->accept();
            }
        });
        answer.start(); window.importCsvText("7");
        QTRY_VERIFY_WITH_TIMEOUT(window.findChild<QPlainTextEdit *>()->toPlainText().contains("CSV import target changed"),5000);
        answer.stop(); QVERIFY(invalidated); QCOMPARE(changed.count(),0);
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
        for(auto *action:window.findChildren<QAction *>()) if(action->objectName()=="importAction") import=action;
        QVERIFY(import); import->trigger();
        QVERIFY(pickerOpened); QVERIFY(refreshed); QVERIFY(fileChosen);
        auto *issues=window.findChild<QPlainTextEdit *>();
        QTRY_VERIFY_WITH_TIMEOUT(issues->toPlainText().contains("Import target changed") || changed.count()>0,10000);
        QCOMPARE(changed.count(),0);
        QVERIFY(issues->toPlainText().contains("Import target changed"));
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
