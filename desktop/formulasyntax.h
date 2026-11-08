// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QColor>
#include <QHash>
#include <QList>
#include <QRegularExpression>
#include <QString>

struct FormulaReferenceCue {int start=0,length=0,r1=0,c1=0,r2=0,c2=0;QString sheet,source;QColor color;};
struct FormulaSyntaxCues {QList<FormulaReferenceCue> references;QList<int> unmatched;int matchFirst=-1,matchLast=-1;bool truncated=false;QString parentheses;};

inline FormulaSyntaxCues formulaSyntaxCues(const QString &text,int cursor,int referenceLimit=64) {
    FormulaSyntaxCues result;if(!text.startsWith('=') || text.size()>65536)return result;
    static const QString endpoint="(?:(?:'((?:[^']|'')+)'|([A-Za-z_][A-Za-z_0-9]*))!)?(\\$?[A-Za-z]{1,3}\\$?[1-9][0-9]*)";
    static const QRegularExpression reference(endpoint+"(?![A-Za-z_0-9]|\\s*\\()(?:\\s*:\\s*"+endpoint+"(?![A-Za-z_0-9]|\\s*\\())?");
    static const QRegularExpression address("\\$?([A-Za-z]{1,3})\\$?([1-9][0-9]*)");
    const QList<QColor> palette{QColor("#0072b2"),QColor("#d55e00"),QColor("#009e73"),QColor("#a83b98"),QColor("#8b6508"),QColor("#5262bd")};
    QList<int> stack;QHash<int,int> pairs;QHash<QString,QColor> colors;
    auto coordinates=[&](const QString &value,int &row,int &col){const auto match=address.match(value);bool ok=false;row=match.captured(2).toInt(&ok);col=0;for(const auto ch:match.captured(1).toUpper())col=col*26+ch.unicode()-'A'+1;return match.hasMatch() && ok && row>=1 && row<=1048576 && col>=1 && col<=16384;};
    for(int i=1;i<text.size();){
        if(text[i]=='"'){++i;while(i<text.size()){if(text[i]=='"'){++i;if(i<text.size() && text[i]=='"'){++i;continue;}break;}++i;}continue;}
        const bool boundary=i==1 || !(text[i-1].isLetterOrNumber() || text[i-1]=='_' || text[i-1]=='$' || text[i-1]=='!');
        if(boundary && (text[i].isLetter() || text[i]=='\'' || text[i]=='$')){const auto match=reference.match(text,i,QRegularExpression::NormalMatch,QRegularExpression::AnchorAtOffsetMatchOption);
            if(match.hasMatch()){FormulaReferenceCue cue;cue.start=i;cue.length=match.capturedLength();cue.source=match.captured();cue.sheet=match.captured(1).isEmpty()?match.captured(2):match.captured(1).replace("''","'");
                bool valid=coordinates(match.captured(3),cue.r1,cue.c1);cue.r2=cue.r1;cue.c2=cue.c1;
                if(!match.captured(6).isEmpty()){const auto rightSheet=match.captured(4).isEmpty()?match.captured(5):match.captured(4).replace("''","'");valid=valid && (rightSheet.isEmpty() || rightSheet.toCaseFolded()==cue.sheet.toCaseFolded()) && coordinates(match.captured(6),cue.r2,cue.c2);}
                if(cue.r1>cue.r2)std::swap(cue.r1,cue.r2);if(cue.c1>cue.c2)std::swap(cue.c1,cue.c2);
                if(valid){const auto key=cue.sheet.toCaseFolded()+QString("/%1/%2/%3/%4").arg(cue.r1).arg(cue.c1).arg(cue.r2).arg(cue.c2);if(!colors.contains(key))colors[key]=palette[colors.size()%palette.size()];cue.color=colors[key];if(result.references.size()<qBound(1,referenceLimit,1024))result.references.append(cue);else result.truncated=true;}
                i+=match.capturedLength();continue;
            }
        }
        if(text[i]=='\''){++i;while(i<text.size()){if(text[i]=='\''){++i;if(i<text.size() && text[i]=='\''){++i;continue;}break;}++i;}continue;}
        if(text[i]=='('){if(stack.size()<128)stack.append(i);else {result.truncated=true;break;}}
        else if(text[i]==')'){if(stack.isEmpty())result.unmatched.append(i);else{const int open=stack.takeLast();pairs[open]=i;pairs[i]=open;}}
        ++i;
    }
    result.unmatched.append(stack);for(int candidate:{cursor-1,cursor})if(pairs.contains(candidate)){result.matchFirst=qMin(candidate,pairs[candidate]);result.matchLast=qMax(candidate,pairs[candidate]);break;}
    if(!result.unmatched.isEmpty())result.parentheses=QString("%1 unmatched parenthesis/parentheses").arg(result.unmatched.size());
    else if(result.matchFirst>=0)result.parentheses=QString("Matching parentheses at %1 and %2").arg(result.matchFirst+1).arg(result.matchLast+1);
    return result;
}
