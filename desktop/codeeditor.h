// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QAbstractItemView>
#include <QCompleter>
#include <QFontDatabase>
#include <QKeyEvent>
#include <QPainter>
#include <QPlainTextEdit>
#include <QRegularExpression>
#include <QScrollBar>
#include <QStringListModel>
#include <QTextBlock>

// Plain text editor for workbook Python: line numbers, four-space
// indentation, line marks and completion of workbook names and context
// methods. It holds text only; nothing here runs code.
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
        m_names=new QStringListModel(this);m_methods=new QStringListModel(contextMethods(),this);
        m_completer=new QCompleter(m_names,this);m_completer->setObjectName("codeCompleter");m_completer->setWidget(this);
        m_completer->setCompletionMode(QCompleter::PopupCompletion);m_completer->setCaseSensitivity(Qt::CaseInsensitive);
        m_completer->setModelSorting(QCompleter::UnsortedModel);
        connect(m_completer,qOverload<const QString &>(&QCompleter::activated),this,&CodeEditor::complete);
    }
    // The ctx methods offered after "ctx.", in the order they are listed.
    static QStringList contextMethods(){return {"get","set","table_delete","table_get","table_insert","table_schema","table_set","value","values"};}
    // Workbook names are offered as bare identifiers; code passes them in quotes.
    void setCompletionNames(QStringList names){names.sort(Qt::CaseInsensitive);m_names->setStringList(names);}
    QStringList completionNames() const{return m_names->stringList();}
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
        auto *popup=m_completer->popup();
        if(popup->isVisible()) {
            const int key=event->key();
            if(key==Qt::Key_Return || key==Qt::Key_Enter || key==Qt::Key_Tab) {
                const auto index=popup->currentIndex().isValid()?popup->currentIndex():m_completer->completionModel()->index(0,0);
                popup->hide();complete(index.data().toString());event->accept();return;
            }
            if(key==Qt::Key_Escape){popup->hide();event->accept();return;}
        }
        if(event->key()==Qt::Key_Tab && !(event->modifiers()&~Qt::KeypadModifier) && !isReadOnly()){insertAtCursor(QStringLiteral("    "));return;}
        QPlainTextEdit::keyPressEvent(event);
        if((event->text().isEmpty() && event->key()!=Qt::Key_Backspace) || (event->modifiers()&(Qt::ControlModifier|Qt::MetaModifier))){popup->hide();return;}
        updateCompletion();
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
    // Where the text before the cursor on this line ends: in code, or inside a
    // string literal (single or double quoted, backslash escapes) whose
    // content so far is returned. A # outside a string starts a comment.
    static bool openString(const QString &line,QString *content) {
        QChar quote;int start=0;
        for(int i=0;i<line.size();++i) {
            const auto ch=line[i];
            if(quote.isNull()) {
                if(ch==u'#')return false;
                if(ch==u'"' || ch==u'\''){quote=ch;start=i+1;}
            } else if(ch==u'\\')++i;
            else if(ch==quote)quote=QChar();
        }
        if(quote.isNull())return false;
        *content=line.mid(start);return true;
    }
    // Offers completions once two or more identifier characters are typed, in
    // the only places they are used: workbook names at the start of a string
    // literal (ctx.value("To…), and context methods directly after "ctx.".
    // Anywhere else no popup appears, so Return and Tab keep their meaning.
    void updateCompletion() {
        auto *popup=m_completer->popup();
        if(isReadOnly()){popup->hide();return;}
        const auto cursor=textCursor();const auto before=cursor.block().text().left(cursor.positionInBlock());
        static const QRegularExpression word(QStringLiteral("([A-Za-z_][A-Za-z0-9_]*)$")),identifier(QStringLiteral("^[A-Za-z_][A-Za-z0-9_]*$")),
            context(QStringLiteral("(?:^|[^A-Za-z0-9_.])ctx\\.$"));
        QString prefix,content;QStringListModel *model=nullptr;
        if(openString(before,&content)) {
            if(identifier.match(content).hasMatch()){prefix=content;model=m_names;}
        } else {
            const auto match=word.match(before);
            if(match.hasMatch() && context.match(before.left(before.size()-match.captured(1).size())).hasMatch()){prefix=match.captured(1);model=m_methods;}
        }
        if(!model || prefix.size()<2){popup->hide();return;}
        if(m_completer->model()!=model)m_completer->setModel(model);
        m_completer->setCompletionPrefix(prefix);
        if(m_completer->completionCount()==0 || (m_completer->completionCount()==1 && m_completer->currentCompletion()==prefix)){popup->hide();return;}
        auto rect=cursorRect();rect.setWidth(popup->sizeHintForColumn(0)+popup->verticalScrollBar()->sizeHint().width());
        m_completer->complete(rect);popup->setCurrentIndex(m_completer->completionModel()->index(0,0));
    }
    void complete(const QString &completion) {
        if(isReadOnly() || completion.isEmpty())return;
        auto cursor=textCursor();cursor.movePosition(QTextCursor::Left,QTextCursor::KeepAnchor,int(m_completer->completionPrefix().size()));
        cursor.insertText(completion);setTextCursor(cursor);
    }
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
    QCompleter *m_completer;
    QStringListModel *m_names,*m_methods;
    QList<QTextEdit::ExtraSelection> m_marks;
};
