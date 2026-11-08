// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "formulasyntax.h"

struct ReferenceCycle {int start=-1,length=0;QString replacement;};

inline ReferenceCycle cycleFormulaReference(const QString &text,int cursor,int selectionStart=-1,int selectionLength=0) {
    ReferenceCycle result;const auto cues=formulaSyntaxCues(text,cursor,1024);
    static const QRegularExpression endpoint("(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z_0-9]*)!)?(\\$?[A-Za-z]{1,3}\\$?[1-9][0-9]*)");
    static const QRegularExpression cell("^(\\$?)([A-Za-z]{1,3})(\\$?)([1-9][0-9]*)$");
    struct Edit{int start,length;QString replacement;};QList<Edit> edits;
    for(const auto &cue:cues.references){auto matches=endpoint.globalMatch(cue.source);while(matches.hasNext()){
        const auto match=matches.next();const int start=cue.start+match.capturedStart(1),length=match.capturedLength(1),end=start+length;
        bool chosen=selectionLength>0?start>=selectionStart && end<=selectionStart+selectionLength:cursor>=start && cursor<=end;
        if(selectionLength>0 && selectionStart<end && selectionStart+selectionLength>start && !chosen)return {};
        if(!chosen)continue;const auto parts=cell.match(match.captured(1));bool column=!parts.captured(1).isEmpty(),row=!parts.captured(3).isEmpty();
        if(!column && !row){column=true;row=true;}else if(column && row){column=false;}else if(row){column=true;row=false;}else column=false;
        edits.append({start,length,(column?QString("$"):QString())+parts.captured(2)+(row?QString("$"):QString())+parts.captured(4)});
    }}
    if(edits.isEmpty())return result;
    result.start=edits.first().start;result.length=edits.last().start+edits.last().length-result.start;result.replacement=text.mid(result.start,result.length);
    for(auto it=edits.crbegin();it!=edits.crend();++it)result.replacement.replace(it->start-result.start,it->length,it->replacement);
    return result;
}
