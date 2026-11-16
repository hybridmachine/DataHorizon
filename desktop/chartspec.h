// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include <QHash>
#include <QJsonObject>
#include <QPoint>
#include <QRect>
#include <QSize>
#include <QString>

// Pure helpers over chart specifications as the service's catalog returns
// them (stored form) and as chart_create / chart_update accept them (input
// form). No widgets and no network.
namespace ChartSpec {

// The input-form spec of a catalog chart: sources are rebuilt from ids.
// Empty, with reason set, when the chart has an issue or a broken source.
QJsonObject inputSpec(const QJsonObject &chart,QString *reason=nullptr);
bool hasBrokenSource(const QJsonObject &chart);
// As inputSpec, but a broken source stays in its stored form (with
// "broken": true) instead of emptying the result, so the panel can show it
// and replace it. Empty only for a chart with an issue.
QJsonObject workingSpec(const QJsonObject &chart);
// True when a working spec still holds a stored broken source.
bool holdsBrokenSource(const QJsonObject &spec);
// Why a chart cannot be drawn; empty for a readable chart.
QString stateReason(const QJsonObject &chart);

// The spec with its type changed and the companion fields adjusted. Empty,
// with refusal set, when the change needs an X source that is missing.
QJsonObject withType(const QJsonObject &spec,const QString &type,QString *refusal=nullptr);
bool takesGrouping(const QString &type);

QJsonObject sourceToInput(const QJsonObject &storedSource);
// "Sheet1!A2:A13", "Table.Column" or "Query.Column".
QString sourceText(const QJsonObject &storedSource,const QHash<QString,QString> &sheetNames);
// A range source, stored or input form, as a range field shows it: the bare
// reference ("B2:B13") on ownSheetId, sheet-qualified as sourceText otherwise;
// empty for a broken range that keeps no rectangle.
QString rangeText(const QJsonObject &source,const QHash<QString,QString> &sheetNames,const QString &ownSheetId);
QJsonObject rangeSource(const QString &sheetId,int r1,int c1,int r2,int c2);
// Accepts "A2:A13", "Sheet1!A2:A13" and "'Q1 Sales'!$A$2:$A$13".
bool parseRange(const QString &text,const QHash<QString,QString> &sheetIdsByName,const QString &currentSheetId,QJsonObject *source);

QSize clampSize(QSize size);
// Handles are numbered clockwise from top-left: 0 top-left, 1 top,
// 2 top-right, 3 right, 4 bottom-right, 5 bottom, 6 bottom-left, 7 left.
QRect resized(QRect start,int handle,QPoint delta);

}
