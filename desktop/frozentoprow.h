// SPDX-License-Identifier: GPL-3.0-only
#pragma once
#include "frozenpanes.h"
// Source-compatible names for the original top-row experiment/tests.
class FrozenTopRowModel : public FrozenPrefixModel {
public:
    explicit FrozenTopRowModel(QAbstractItemModel *source,QObject *parent=nullptr):FrozenPrefixModel(source,1,-1,parent){}
};
using FrozenTopRow=FrozenPanes;
