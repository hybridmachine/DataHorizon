// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include <QJsonObject>
#include <QPainter>
#include <QStringList>

inline void paintCellBorders(QPainter *painter,const QRect &rect,const QJsonObject &style,const QColor &color) {
    painter->save();painter->setClipRect(rect);painter->setBrush(Qt::NoBrush);
    for(const auto &side:QStringList{"left","right","top","bottom"}) {
        const auto edge=style["border_"+side].toString(style["border"].toString()=="box"?"thin":"none");if(edge=="none")continue;
        const qreal width=edge=="thick"?4:edge=="medium"?3:2;const auto pattern=edge=="dashed"?Qt::DashLine:edge=="dotted"?Qt::DotLine:Qt::SolidLine;
        painter->setPen(QPen(color,width,pattern,Qt::FlatCap));const int inset=width>2?2:1;const auto box=rect.adjusted(inset,inset,-inset-1,-inset-1);
        if(side=="left")painter->drawLine(box.topLeft(),box.bottomLeft());else if(side=="right")painter->drawLine(box.topRight(),box.bottomRight());else if(side=="top")painter->drawLine(box.topLeft(),box.topRight());else painter->drawLine(box.bottomLeft(),box.bottomRight());
    }
    painter->restore();
}
