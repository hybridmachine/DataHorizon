// SPDX-License-Identifier: GPL-3.0-only
#pragma once

#include "restclient.h"
#include <QAbstractTableModel>
#include <QCache>
#include <QSet>
#include <QRect>
#include "viewportcalculations.h"

class SheetModel : public QAbstractTableModel {
    Q_OBJECT
public:
    enum { CellRole=Qt::UserRole+1 };
    explicit SheetModel(RestClient *client, QObject *parent = nullptr, int rows = 1048576, int cols = 16384);
    int rowCount(const QModelIndex &parent = {}) const override;
    int columnCount(const QModelIndex &parent = {}) const override;
    QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const override;
    QVariant headerData(int section, Qt::Orientation orientation, int role) const override;
    Qt::ItemFlags flags(const QModelIndex &index) const override;
    bool setData(const QModelIndex &index, const QVariant &value, int role) override;
    void bind(const QString &workbook, const QString &sheet, const QString &state, bool readonly);
    void setBusy(bool value);
    void setProjection(const QList<int> &rows);
    bool projected() const { return m_projected; }
    int sourceRow(int row) const { return m_projected ? m_projection.value(row, -1) : row; }
    int viewRow(int row) const { return m_projected ? int(m_projection.indexOf(row)) : row; }
    QJsonObject cell(const QModelIndex &index) const;
    bool loaded(const QModelIndex &index) const;
    bool sourceReady(const QModelIndex &index) const;
    void ensureSource(const QModelIndex &index) const;
    QString workbook() const { return m_workbook; }
    QString sheet() const { return m_sheet; }
    QString state() const { return m_state; }
    QJsonObject cacheMetrics() const {
        return {{"tiles",int(m_cache.size())},{"tile_charge_bytes",int(m_cache.totalCost())},
                {"tile_budget_bytes",int(m_cache.maxCost())},{"source_charge_bytes",int(m_sources.totalCost())},
                {"source_budget_bytes",int(m_sources.maxCost())},{"pending_tiles",int(m_loading.size())},
                {"queued_calculation_tiles",m_background->queued()},{"active_viewport_calculation",m_background->active()}};
    }
    static QString columnName(int oneBased);
    void setHiddenSections(const QSet<int> &rows,const QSet<int> &columns);
    void setVisibleRanges(const QList<QRect> &ranges);
    void setBackgroundEnabled(bool enabled);
signals:
    void editRequested(int row, int col, QString source, QString kind);
    void issue(QString message);
    void externalChange();
    void tileLoaded();
private:
    struct Tile { QHash<int, QJsonObject> cells; };
    void fetch(int tileRow, int tileCol) const;
    static quint64 tileKey(int tileRow, int tileCol);
    bool needsCalculation(quint64 key) const;
    void queueCalculation(quint64 key) const;
    void calculated(const ViewportCalculations::Work &work,const QJsonObject &snapshot,const QString &error);
    ViewportCalculations *m_background;
    RestClient *m_client;
    QString m_workbook, m_sheet, m_state;
    bool m_readonly = true, m_busy = false;
    int m_generation = 0;
    int m_rows, m_cols;
    bool m_projected = false;
    QList<int> m_projection;
    QSet<int> m_hiddenRows, m_hiddenColumns;
    mutable QCache<quint64, Tile> m_cache{14*1024*1024};
    mutable QCache<quint64, QString> m_sources{2*1024*1024};
    mutable QSet<quint64> m_sourceLoading;
    mutable QSet<quint64> m_loading;
};
