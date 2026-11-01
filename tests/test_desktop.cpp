// SPDX-License-Identifier: GPL-3.0-only
#include "mainwindow.h"
#include <QAbstractItemModelTester>
#include <QApplication>
#include <QAction>
#include <QJsonArray>
#include <QLineEdit>
#include <QLabel>
#include <QSignalSpy>
#include <QTableView>
#include <QPlainTextEdit>
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
};

QTEST_MAIN(DesktopTests)
#include "test_desktop.moc"
