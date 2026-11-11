// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QPalette>
#include <QRegularExpression>
#include <QSyntaxHighlighter>
#include <QTextCharFormat>

// Display-only Python colouring. It reads the editor text and never imports,
// compiles or executes it.
class PythonHighlighter : public QSyntaxHighlighter {
public:
    explicit PythonHighlighter(QTextDocument *document):QSyntaxHighlighter(document) {
        const bool dark=QPalette().color(QPalette::Base).lightness()<128;
        auto format=[](const QColor &color,bool bold=false,bool italic=false){QTextCharFormat result;result.setForeground(color);if(bold)result.setFontWeight(QFont::Bold);result.setFontItalic(italic);return result;};
        m_keyword=format(dark?QColor(0x82,0xaa,0xff):QColor(0x1f,0x4e,0xb4),true);
        m_definition=format(dark?QColor(0xff,0xcb,0x6b):QColor(0x8a,0x4b,0x00),true);
        m_number=format(dark?QColor(0xf7,0x8c,0x6c):QColor(0x9c,0x2c,0x00));
        m_string=format(dark?QColor(0xc3,0xe8,0x8d):QColor(0x2e,0x7d,0x32));
        m_comment=format(dark?QColor(0x9e,0x9e,0x9e):QColor(0x6b,0x6b,0x6b),false,true);
        static const QStringList keywords{"False","None","True","and","as","assert","async","await","break","class","continue","def","del","elif","else","except",
            "finally","for","from","global","if","import","in","is","lambda","nonlocal","not","or","pass","raise","return","try","while","with","yield","match","case"};
        m_keywords=QRegularExpression("\\b(?:"+keywords.join('|')+")\\b");
        m_names=QRegularExpression("\\b(?:def|class)\\s+([A-Za-z_][A-Za-z0-9_]*)");
        m_numbers=QRegularExpression("\\b(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+|[0-9][0-9_]*(?:\\.[0-9_]*)?(?:[eE][+-]?[0-9]+)?[jJ]?)\\b");
    }
protected:
    void highlightBlock(const QString &text) override {
        for(auto match=m_keywords.globalMatch(text);match.hasNext();){const auto item=match.next();setFormat(int(item.capturedStart()),int(item.capturedLength()),m_keyword);}
        for(auto match=m_numbers.globalMatch(text);match.hasNext();){const auto item=match.next();setFormat(int(item.capturedStart()),int(item.capturedLength()),m_number);}
        for(auto match=m_names.globalMatch(text);match.hasNext();){const auto item=match.next();setFormat(int(item.capturedStart(1)),int(item.capturedLength(1)),m_definition);}
        // Strings and comments are scanned left to right so quotes inside
        // comments and hashes inside strings are coloured correctly. Block
        // state 1/2 carries an open ''' or """ string to the next line.
        int position=0;const int length=int(text.size());
        QString open=previousBlockState()==1?QStringLiteral("'''"):previousBlockState()==2?QStringLiteral("\"\"\""):QString();
        setCurrentBlockState(0);
        while(position<length) {
            if(!open.isEmpty()) {
                const int end=int(text.indexOf(open,position));
                if(end<0){setFormat(position,length-position,m_string);setCurrentBlockState(open.startsWith('\'')?1:2);return;}
                setFormat(position,end+3-position,m_string);position=end+3;open.clear();continue;
            }
            const QChar ch=text[position];
            if(ch=='#'){setFormat(position,length-position,m_comment);return;}
            if(ch=='\'' || ch=='"') {
                const QString triple(3,ch);
                if(text.mid(position,3)==triple){setFormat(position,3,m_string);open=triple;position+=3;continue;}
                int end=position+1;
                while(end<length && text[end]!=ch){if(text[end]=='\\')++end;++end;}
                setFormat(position,qMin(end+1,length)-position,m_string);position=end+1;continue;
            }
            ++position;
        }
    }
private:
    QTextCharFormat m_keyword,m_definition,m_number,m_string,m_comment;
    QRegularExpression m_keywords,m_names,m_numbers;
};
