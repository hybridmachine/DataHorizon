// SPDX-License-Identifier: GPL-3.0-only
#include "sheetmodel.h"
#include <QBrush>
#include <QFont>
#include <QJsonArray>
#include <QJsonDocument>
#include <QTimer>

SheetModel::SheetModel(RestClient *client, QObject *parent, int rows, int cols) : QAbstractTableModel(parent), m_client(client), m_rows(rows), m_cols(cols) {
    setObjectName("sheetModel");
}
int SheetModel::rowCount(const QModelIndex &parent) const { return parent.isValid() ? 0 : m_projected ? int(m_projection.size()) : m_rows; }
int SheetModel::columnCount(const QModelIndex &parent) const { return parent.isValid() ? 0 : m_cols; }
quint64 SheetModel::tileKey(int row, int col) { return (quint64(row) << 32) | quint32(col); }

QString SheetModel::columnName(int col) {
    QString name;
    while (col) { name.prepend(QChar('A' + (col - 1) % 26)); col = (col - 1) / 26; }
    return name;
}

QVariant SheetModel::headerData(int section, Qt::Orientation orientation, int role) const {
    if (role != Qt::DisplayRole) return {};
    return orientation == Qt::Horizontal ? QVariant(columnName(section + 1)) : QVariant(sourceRow(section) + 1);
}

bool SheetModel::loaded(const QModelIndex &index) const {
    return index.isValid() && m_cache.contains(tileKey(index.row()/32, index.column()/16));
}

QJsonObject SheetModel::cell(const QModelIndex &index) const {
    if (!index.isValid()) return {};
    const int row = index.row();
    auto *tile = m_cache.object(tileKey(row/32, index.column()/16));
    if (!tile) { fetch(row/32, index.column()/16); return {}; }
    auto item=tile->cells.value((row%32)*16 + index.column()%16);
    if (item["source_omitted"].toBool()) {
        if (auto *source=m_sources.object(tileKey(sourceRow(row)+1,index.column()+1))) item["source"]=*source;
    }
    return item;
}

bool SheetModel::sourceReady(const QModelIndex &index) const {
    if (!loaded(index)) return false;
    return !cell(index)["source_omitted"].toBool() || m_sources.contains(tileKey(sourceRow(index.row())+1,index.column()+1));
}

void SheetModel::ensureSource(const QModelIndex &index) const {
    if (!loaded(index) || sourceReady(index)) return;
    const int row=sourceRow(index.row())+1, col=index.column()+1, generation=m_generation;
    const auto key=tileKey(row,col);
    if (m_sourceLoading.contains(key) || m_sourceLoading.size()>=4) return;
    m_sourceLoading.insert(key);
    auto *self=const_cast<SheetModel *>(this);
    m_client->request("/v1/workbooks/"+m_workbook+"/range",{{"sheet",m_sheet},{"r1",row},{"r2",row},{"c1",col},{"c2",col}},
        [self,generation,key,index](QJsonObject result,QString error){
            if (generation!=self->m_generation) return;
            self->m_sourceLoading.remove(key);
            if (!error.isEmpty()) { emit self->issue(error); return; }
            if (result["state"].toString()!=self->m_state) { emit self->externalChange(); return; }
            const auto cells=result["cells"].toArray();
            const auto item=cells.isEmpty()?QJsonObject{}:cells.first().toObject();
            if (cells.size()!=1 || !item["source"].isString() || item["row"].toInt()!=int(key>>32)
                || item["col"].toInt()!=int(quint32(key)) || result["sheet"].toString()!=self->m_sheet) {
                emit self->issue("Incomplete cell source reply; editing remains disabled."); return;
            }
            const auto source=cells.first().toObject()["source"].toString();
            self->m_sources.insert(key,new QString(source),int(source.size()*sizeof(QChar)+128));
            emit self->dataChanged(index,index,{Qt::EditRole});
            emit self->tileLoaded();
        });
}

QVariant SheetModel::data(const QModelIndex &index, int role) const {
    if (!index.isValid()) return {};
    auto item = cell(index);
    auto style = item.value("style").toObject();
    switch (role) {
    case Qt::DisplayRole: return loaded(index) ? item.value("display").toString() : QStringLiteral("…");
    case Qt::EditRole:
        if (!sourceReady(index)) { ensureSource(index); return {}; }
        return item.value("source").toString();
    case Qt::ToolTipRole: return item.value("error").toString(item.value("source").toString());
    case Qt::AccessibleTextRole: return columnName(index.column()+1) + QString::number(sourceRow(index.row())+1) + ": " + item.value("display").toString();
    case Qt::FontRole: {
        QFont font; font.setBold(style.value("bold").toBool()); font.setItalic(style.value("italic").toBool()); return font;
    }
    case Qt::BackgroundRole: if (style.contains("background")) return QBrush(QColor(style.value("background").toString())); break;
    case Qt::ForegroundRole: if (item.contains("error")) return QBrush(QColor("#b3261e")); break;
    case Qt::TextAlignmentRole: {
        auto align = style.value("align").toString();
        auto horizontal = align == "center" ? Qt::AlignHCenter : align == "right" || (align.isEmpty() && item.value("value").isDouble()) ? Qt::AlignRight : Qt::AlignLeft;
        return int(horizontal | Qt::AlignVCenter);
    }
    }
    return {};
}

Qt::ItemFlags SheetModel::flags(const QModelIndex &index) const {
    if (!index.isValid()) return Qt::NoItemFlags;
    auto result = Qt::ItemIsEnabled | Qt::ItemIsSelectable;
    if (!m_readonly && !m_busy && sourceReady(index)) result |= Qt::ItemIsEditable;
    return result;
}

bool SheetModel::setData(const QModelIndex &index, const QVariant &value, int role) {
    if (role != Qt::EditRole || !(flags(index) & Qt::ItemIsEditable)) return false;
    const auto kind=cell(index)["input_kind"]=="text"?QString("text"):QString("auto");
    emit editRequested(sourceRow(index.row())+1, index.column()+1, value.toString(),kind);
    return true;
}

void SheetModel::bind(const QString &workbook, const QString &sheet, const QString &state, bool readonly) {
    beginResetModel();
    ++m_generation; m_cache.clear(); m_loading.clear();
    m_sources.clear(); m_sourceLoading.clear();
    m_projected = false; m_projection.clear();
    m_workbook = workbook; m_sheet = sheet; m_state = state; m_readonly = readonly;
    endResetModel();
}

void SheetModel::setBusy(bool value) { m_busy = value; }

void SheetModel::setProjection(const QList<int> &rows) {
    beginResetModel(); ++m_generation; m_cache.clear(); m_loading.clear(); m_sources.clear(); m_sourceLoading.clear(); m_projected = true; m_projection = rows; endResetModel();
}

void SheetModel::fetch(int tileRow, int tileCol) const {
    auto key = tileKey(tileRow, tileCol);
    if (m_workbook.isEmpty() || m_loading.contains(key) || m_loading.size() >= 4) return;
    m_loading.insert(key);
    auto *self = const_cast<SheetModel *>(this);
    const int generation = m_generation;
    QTimer::singleShot(0, self, [self, key, tileRow, tileCol, generation] {
        if (generation != self->m_generation) return;
        QJsonObject body{{"sheet", self->m_sheet}, {"r1", tileRow*32+1}, {"c1", tileCol*16+1},
                         {"r2", qMin(tileRow*32+32, 1048576)}, {"c2", qMin(tileCol*16+16, 16384)}, {"preview",true}};
        if (self->m_projected) {
            QJsonArray rows;
            for (int row=tileRow*32; row<qMin(tileRow*32+32, int(self->m_projection.size())); ++row) rows.append(self->m_projection[row]+1);
            if (rows.isEmpty()) { self->m_loading.remove(key); return; }
            body["rows"] = rows;
        }
        self->m_client->request("/v1/workbooks/" + self->m_workbook + "/range", body,
            [self, generation, key, tileRow, tileCol](QJsonObject result, QString error) {
            if (generation != self->m_generation) return;
            self->m_loading.remove(key);
            if (!error.isEmpty()) { emit self->issue(error); return; }
            if (result.value("state").toString() != self->m_state) { emit self->externalChange(); return; }
            auto *tile = new Tile;
            for (const auto &value : result.value("cells").toArray()) {
                auto cell = value.toObject();
                const int sourceRow = cell.value("row").toInt()-1;
                const int row = self->m_projected ? int(self->m_projection.indexOf(sourceRow)) : sourceRow;
                tile->cells.insert((row%32)*16 + (cell.value("col").toInt()-1)%16, cell);
            }
            // Bound both tile count and estimated memory, including JSON overhead.
            if (self->m_cache.size() >= 16) self->m_cache.remove(self->m_cache.keys().last());
            const auto cost = qMax(1024, int(QJsonDocument(result).toJson(QJsonDocument::Compact).size()*3));
            self->m_cache.insert(key, tile, cost);
            if (self->m_projected) {
                if (self->rowCount()) emit self->dataChanged(self->index(0, tileCol*16), self->index(self->rowCount()-1, qMin(tileCol*16+15, self->m_cols-1)));
            } else {
                emit self->dataChanged(self->index(tileRow*32, tileCol*16), self->index(qMin(tileRow*32+31, self->m_rows-1), qMin(tileCol*16+15, self->m_cols-1)));
            }
            emit self->tileLoaded();
        });
    });
}
