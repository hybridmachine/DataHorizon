// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include <QAbstractItemModelTester>
#include <QApplication>
#include <QAction>
#include <QJsonArray>
#include <QLineEdit>
#include <QItemSelectionModel>
#include <QRegularExpression>
#include <QLabel>
#include <QSignalSpy>
#include <QTableView>
#include <QPlainTextEdit>
#include <QAbstractButton>
#include <QAbstractItemDelegate>
#include <QClipboard>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QJsonDocument>
#include <QMessageBox>
#include <QMimeData>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTabBar>
#include <QTimer>
#include <QStyleOptionViewItem>
#include <QTemporaryDir>
#include <QTest>

class DesktopTests : public QObject {
    Q_OBJECT
private slots:
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
        QCOMPARE(model->data(model->index(0,0)).toString(),QString("8"));
        QCOMPARE(model->data(model->index(0,1)).toString(),QString("16"));
        QVERIFY(window.findChild<QPlainTextEdit *>()->toPlainText().contains("nonnegative"));
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
        auto *editor=grid->itemDelegate()->createEditor(grid->viewport(),QStyleOptionViewItem{},model->index(0,0));
        grid->itemDelegate()->setEditorData(editor,model->index(0,0));
        auto *line=qobject_cast<QLineEdit *>(editor); QVERIFY(line); QCOMPARE(line->text(),original);
        line->setCursorPosition(line->text().size()); line->insert("z");
        grid->itemDelegate()->setModelData(editor,model,model->index(0,0)); delete editor;
        QTRY_COMPARE_WITH_TIMEOUT(opened.count(),3,10000);
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
    void invalidHttpAcknowledgementsAreErrors() {
        QTcpServer server; QVERIFY(server.listen(QHostAddress::LocalHost,0));
        QByteArray payload;
        connect(&server,&QTcpServer::newConnection,this,[&]{
            auto *socket=server.nextPendingConnection();
            connect(socket,&QTcpSocket::disconnected,socket,&QObject::deleteLater);
            connect(socket,&QTcpSocket::readyRead,socket,[socket,&payload]{
                auto request=socket->property("request").toByteArray()+socket->readAll(); socket->setProperty("request",request);
                if(!request.contains("\r\n\r\n")) return;
                socket->write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "+QByteArray::number(payload.size())+"\r\n\r\n"+payload);
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
        QJsonObject receipt{{"operation_id","operation"},{"outcome","committed"},{"state","state:1"},{"changed_cells",0}};
        QVERIFY(RestClient::validReceipt(receipt,"operation",true));
        QVERIFY(!RestClient::validReceipt(receipt,"other",true)); receipt.remove("state"); QVERIFY(!RestClient::validReceipt(receipt,"operation",true));
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
private:
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
