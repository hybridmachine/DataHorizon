// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QFontDatabase>
#include <QKeyEvent>
#include <QPainter>
#include <QPlainTextEdit>
#include <QTextBlock>

// Plain text editor for workbook Python: line numbers, four-space
// indentation and line marks. It holds text only; nothing here runs code.
class CodeEditor : public QPlainTextEdit {
    Q_OBJECT
public:
    explicit CodeEditor(QWidget *parent=nullptr):QPlainTextEdit(parent),m_numbers(new LineNumbers(this)) {
        setFont(QFontDatabase::systemFont(QFontDatabase::FixedFont));
        setLineWrapMode(QPlainTextEdit::NoWrap);
        setTabChangesFocus(false);
        setTabStopDistance(fontMetrics().horizontalAdvance(QLatin1Char(' '))*4);
        connect(this,&QPlainTextEdit::blockCountChanged,this,[this]{updateMargin();});
        connect(this,&QPlainTextEdit::updateRequest,this,[this](const QRect &rect,int dy){
            if(dy)m_numbers->scroll(0,dy);else m_numbers->update(0,rect.y(),m_numbers->width(),rect.height());
            if(rect.contains(viewport()->rect()))updateMargin();});
        updateMargin();
    }
    // Lines are 1-based, matching tracebacks and service messages.
    void markLine(int line,bool error) {
        const auto block=document()->findBlockByNumber(line-1);if(!block.isValid())return;
        QTextEdit::ExtraSelection mark;mark.format.setBackground(error?QColor(255,80,80,70):QColor(255,200,0,70));
        mark.format.setProperty(QTextFormat::FullWidthSelection,true);mark.cursor=QTextCursor(block);
        m_marks.append(mark);setExtraSelections(m_marks);
    }
    void clearMarks(){m_marks.clear();setExtraSelections(m_marks);}
    void insertAtCursor(const QString &text){if(!isReadOnly())textCursor().insertText(text);}
    int lineNumberWidth() const {
        int digits=1;for(int max=qMax(1,blockCount());max>=10;max/=10)++digits;
        return 8+fontMetrics().horizontalAdvance(QLatin1Char('9'))*digits;
    }
protected:
    void keyPressEvent(QKeyEvent *event) override {
        if(event->key()==Qt::Key_Tab && !(event->modifiers()&~Qt::KeypadModifier) && !isReadOnly()){insertAtCursor(QStringLiteral("    "));return;}
        QPlainTextEdit::keyPressEvent(event);
    }
    void resizeEvent(QResizeEvent *event) override {
        QPlainTextEdit::resizeEvent(event);const auto area=contentsRect();
        m_numbers->setGeometry(QRect(area.left(),area.top(),lineNumberWidth(),area.height()));
    }
private:
    class LineNumbers : public QWidget {
    public:
        explicit LineNumbers(CodeEditor *editor):QWidget(editor),m_editor(editor){setObjectName("codeLineNumbers");}
        QSize sizeHint() const override{return {m_editor->lineNumberWidth(),0};}
    protected:
        void paintEvent(QPaintEvent *event) override{m_editor->paintNumbers(event);}
    private:
        CodeEditor *m_editor;
    };
    void updateMargin(){setViewportMargins(lineNumberWidth(),0,0,0);}
    void paintNumbers(QPaintEvent *event) {
        QPainter painter(m_numbers);painter.fillRect(event->rect(),palette().color(QPalette::AlternateBase));
        painter.setPen(palette().color(QPalette::PlaceholderText));
        auto block=firstVisibleBlock();int top=qRound(blockBoundingGeometry(block).translated(contentOffset()).top());
        while(block.isValid() && top<=event->rect().bottom()) {
            const int bottom=top+qRound(blockBoundingRect(block).height());
            if(block.isVisible() && bottom>=event->rect().top())
                painter.drawText(0,top,m_numbers->width()-4,fontMetrics().height(),Qt::AlignRight,QString::number(block.blockNumber()+1));
            block=block.next();top=bottom;
        }
    }
    LineNumbers *m_numbers;
    QList<QTextEdit::ExtraSelection> m_marks;
};
