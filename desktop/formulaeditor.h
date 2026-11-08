// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QAbstractItemView>
#include <QCompleter>
#include <QJsonArray>
#include <QJsonObject>
#include <QKeyEvent>
#include <QLabel>
#include <QLineEdit>
#include <QRegularExpression>
#include <QStringListModel>
#include <QTimer>
#include <QPainter>
#include <QScreen>
#include <QStyleOptionFrame>
#include <QTextLayout>
#include <functional>
#include "formulasyntax.h"
#include "referencecycle.h"

struct FormulaCursorContext {
    int start=-1,end=-1,argument=0;QString prefix,function;bool quoted=false;
};

inline FormulaCursorContext formulaCursorContext(const QString &text,int cursor) {
    FormulaCursorContext result;if(!text.startsWith('=') || text.size()>65536)return result;
    cursor=qBound(1,cursor,text.size());struct Call{QString name;int argument;};QList<Call> calls;QChar quote;
    auto nameChar=[](QChar ch){return ch.isLetterOrNumber() || ch=='_';};
    for(int i=1;i<cursor;++i){const auto ch=text[i];if(!quote.isNull()){if(ch==quote){if(i+1<cursor && text[i+1]==quote)++i;else quote=QChar();}continue;}
        if(ch=='"' || ch=='\''){quote=ch;continue;}if(ch=='('){int end=i;while(end>1 && text[end-1].isSpace())--end;int start=end;while(start>1 && nameChar(text[start-1]))--start;calls.append({text.mid(start,end-start).toUpper(),0});}
        else if(ch==')'){if(!calls.isEmpty())calls.removeLast();}else if(ch==',' && !calls.isEmpty())++calls.last().argument;
    }
    result.quoted=!quote.isNull();if(result.quoted)return result;
    for(auto it=calls.crbegin();it!=calls.crend();++it)if(!it->name.isEmpty()){result.function=it->name;result.argument=it->argument+1;break;}
    int start=cursor;while(start>1 && nameChar(text[start-1]))--start;int end=cursor;while(end<text.size() && nameChar(text[end]))++end;
    int before=start;while(before>1 && text[before-1].isSpace())--before;
    if(before==1 || QString("(,+-*/^&=<>:").contains(text[before-1])){result.start=start;result.end=end;result.prefix=text.mid(start,cursor-start);}
    return result;
}

class FormulaLineEdit : public QLineEdit {
public:
    explicit FormulaLineEdit(QWidget *parent=nullptr):QLineEdit(parent),m_completer(new QCompleter(this)),m_names(new QStringListModel(this)),m_hint(new QLabel(this,Qt::ToolTip)) {
        setMaxLength(65536);m_completer->setWidget(this);m_completer->setModel(m_names);m_completer->setCaseSensitivity(Qt::CaseInsensitive);m_completer->setCompletionMode(QCompleter::PopupCompletion);m_completer->setMaxVisibleItems(8);m_completer->popup()->setAccessibleName(tr("Formula function suggestions"));
        m_hint->setObjectName("formulaArgumentHint");m_hint->setTextFormat(Qt::PlainText);m_hint->setWordWrap(true);m_hint->setMaximumWidth(600);m_hint->setMargin(5);m_hint->setAccessibleName(tr("Formula argument hint"));m_hint->setAttribute(Qt::WA_ShowWithoutActivating);m_hint->hide();
        connect(m_completer,qOverload<const QString &>(&QCompleter::activated),this,[this](const QString &name){completeFunction(name);});
        connect(this,&QLineEdit::textChanged,this,[this]{updateSyntax();});connect(this,&QLineEdit::cursorPositionChanged,this,[this]{updateSyntax();});
        connect(this,&QLineEdit::textEdited,this,[this]{updateAssistance(true);});connect(this,&QLineEdit::cursorPositionChanged,this,[this]{updateAssistance(false);});
    }
    void setCatalog(const QJsonObject &catalog){m_functions={};QStringList names;for(const auto &value:catalog["functions"].toArray()){const auto f=value.toObject();const auto name=f["name"].toString();if(!name.isEmpty()){m_functions[name]=f;names.append(name);}}names.sort();m_names->setStringList(names);dismissAssistance();}
    QStringList functionNames()const{return m_names->stringList();}
    void setFormulaAllowed(bool allowed){m_allowed=allowed;if(!allowed)dismissAssistance();updateSyntax();}
    bool formulaAllowed()const{return m_allowed;}
    std::function<void()> syntaxChanged;
    FormulaSyntaxCues syntaxCues()const{return m_syntax;}
    void updateSyntax(){m_syntax=m_allowed?formulaSyntaxCues(text(),cursorPosition()):FormulaSyntaxCues{};setProperty("formulaReferenceCount",m_syntax.references.size());setProperty("formulaParentheses",m_syntax.parentheses);setAccessibleDescription(argumentHint()+"\n"+m_syntax.parentheses);update();if(syntaxChanged)syntaxChanged();}
    QString argumentHint()const{return property("formulaArgumentHint").toString();}
    bool completionVisible()const{return m_completer->popup()->isVisible();}
    void dismissAssistance(){m_completer->popup()->hide();m_hint->hide();setProperty("formulaArgumentHint",QString());setAccessibleDescription(m_syntax.parentheses);}
    bool completionKey(const QKeyEvent *key){
        if(!completionVisible())return false;
        if(key->key()==Qt::Key_Escape){dismissAssistance();return true;}
        if(key->key()==Qt::Key_Return || key->key()==Qt::Key_Enter || key->key()==Qt::Key_Tab){auto index=m_completer->popup()->currentIndex();if(!index.isValid())index=m_completer->completionModel()->index(0,0);completeFunction(index.data().toString());return true;}
        if(key->key()==Qt::Key_Up || key->key()==Qt::Key_Down){const int count=m_completer->completionCount();if(count){int row=m_completer->popup()->currentIndex().row();row=(row+(key->key()==Qt::Key_Up?-1:1)+count)%count;m_completer->popup()->setCurrentIndex(m_completer->completionModel()->index(row,0));}return true;}return false;
    }
    bool completeFunction(const QString &name){
        if(!editableFormula() || !m_functions.contains(name))return false;const auto context=formulaCursorContext(text(),cursorPosition());if(context.start<0 || context.quoted)return false;
        const auto suffix=text().mid(context.end);const auto insertion=name+(suffix.trimmed().startsWith('(')?QString():QString("("));
        m_updating=true;setSelection(context.start,context.end-context.start);insert(insertion);m_updating=false;m_completer->popup()->hide();updateAssistance(false);return true;
    }
    bool cycleReference(){
        if(!editableFormula())return false;
        const auto change=cycleFormulaReference(text(),cursorPosition(),selectionStart(),selectedText().size());if(change.start<0)return false;
        if(text().size()-change.length+change.replacement.size()>maxLength() || (text().left(change.start)+change.replacement+text().mid(change.start+change.length)).toUtf8().size()>65536)return false;
        dismissAssistance();setCursorPosition(0);setSelection(change.start,change.length);insert(change.replacement);updateAssistance(false);return true;
    }
    void updateAssistance(bool suggestions){
        if(m_updating)return;if(!editableFormula()){dismissAssistance();return;}const auto context=formulaCursorContext(text(),cursorPosition());QString hint;
        if(!context.quoted && m_functions.contains(context.function)){const auto f=m_functions.value(context.function).toObject();hint=f["signature"].toString()+tr(" • Argument %1").arg(context.argument)+"\n"+f["summary"].toString();}
        setProperty("formulaArgumentHint",hint);setAccessibleDescription(hint+"\n"+m_syntax.parentheses);if(hint.isEmpty())m_hint->hide();else{m_hint->setText(hint);m_hint->adjustSize();auto point=mapToGlobal(QPoint(0,height()+2));const auto bounds=screen()->availableGeometry();point.setX(qBound(bounds.left(),point.x(),qMax(bounds.left(),bounds.right()-m_hint->width()+1)));point.setY(qBound(bounds.top(),point.y(),qMax(bounds.top(),bounds.bottom()-m_hint->height()+1)));m_hint->move(point);m_hint->show();}
        if(context.start<0 || context.quoted || context.prefix.isEmpty()){m_completer->popup()->hide();return;}m_completer->setCompletionPrefix(context.prefix);
        if(suggestions && m_completer->completionCount()){m_completer->complete();m_completer->popup()->setCurrentIndex(m_completer->completionModel()->index(0,0));}else if(!m_completer->completionCount())m_completer->popup()->hide();
    }
protected:
    void paintEvent(QPaintEvent *event)override {
        QLineEdit::paintEvent(event);if(!m_allowed || !text().startsWith('=') || echoMode()!=QLineEdit::Normal)return;
        QTextLayout layout(displayText(),font());QTextOption textOption;textOption.setTextDirection(layoutDirection());layout.setTextOption(textOption);layout.beginLayout();auto line=layout.createLine();line.setLineWidth(1e9);layout.endLayout();if(!line.isValid())return;
        QStyleOptionFrame option;initStyleOption(&option);const auto contents=style()->subElementRect(QStyle::SE_LineEditContents,&option,this).adjusted(2+textMargins().left(),1,-2-textMargins().right(),-1);QPainter painter(this);painter.setClipRect(contents);
        const auto caret=cursorRect();const qreal origin=caret.x()+1-line.cursorToX(cursorPosition());
        auto span=[&](int start,int length){const qreal a=origin+line.cursorToX(start),b=origin+line.cursorToX(start+length);return QRectF(qMin(a,b),caret.top()+2,qMax<qreal>(2,qAbs(b-a)),qMax(2,caret.height()-4));};
        for(const auto &cue:m_syntax.references){const auto rect=span(cue.start,cue.length);painter.setPen(QPen(cue.color,2));painter.drawLine(rect.bottomLeft(),rect.bottomRight());}
        painter.setBrush(Qt::NoBrush);painter.setPen(QPen(QColor("#009e73"),1.5));if(m_syntax.matchFirst>=0){painter.drawRect(span(m_syntax.matchFirst,1));painter.drawRect(span(m_syntax.matchLast,1));}
        painter.setPen(QPen(QColor("#d43f3a"),1.5));for(int i=0;i<qMin(16,m_syntax.unmatched.size());++i)painter.drawRect(span(m_syntax.unmatched[i],1));
    }
    void keyPressEvent(QKeyEvent *key)override{if(key->key()==Qt::Key_F4 && key->modifiers()==Qt::NoModifier && cycleReference()){key->accept();return;}if(completionKey(key)){key->accept();return;}if(key->key()==Qt::Key_Space && key->modifiers()==Qt::ControlModifier && editableFormula()){const auto c=formulaCursorContext(text(),cursorPosition());if(c.start>=0 && !c.quoted){m_completer->setCompletionPrefix(c.prefix);m_completer->complete();m_completer->popup()->setCurrentIndex(m_completer->completionModel()->index(0,0));}return;}QLineEdit::keyPressEvent(key);}
    void focusOutEvent(QFocusEvent *event)override{m_hint->hide();QLineEdit::focusOutEvent(event);}
    void hideEvent(QHideEvent *event)override{dismissAssistance();QLineEdit::hideEvent(event);}
    void changeEvent(QEvent *event)override{if(event->type()==QEvent::EnabledChange && !isEnabled())dismissAssistance();QLineEdit::changeEvent(event);}
private:
    bool editableFormula()const{return m_allowed && isEnabled() && !isReadOnly() && window()->focusWidget()==this && text().startsWith('=') && text().size()<=65536;}
    QCompleter *m_completer;QStringListModel *m_names;QLabel *m_hint;QJsonObject m_functions;FormulaSyntaxCues m_syntax;bool m_allowed=true,m_updating=false;
};
