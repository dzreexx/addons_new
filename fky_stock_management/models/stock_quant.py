# -*- coding: utf-8 -*-
import threading

from odoo import api, models

# Request-scoped accumulator of (product_id, location_id) pairs whose stock
# changed. A bulk operation (inventory import, mass adjustment) writes many
# quants in one request; recomputing the affected rules on EVERY write is an
# O(rows) storm of read_groups. Instead we collect the pairs here and flush
# ONCE, lazily, when the environment cache is invalidated (end of request /
# commit / rollback).
#
# Thread-local, not a plain global: Odoo serves concurrent requests on
# multiple threads, and each thread must keep its own dirty set. The set is
# always cleared in ``_invalidate_cache`` so nothing leaks between requests.
_FKY_DIRTY = threading.local()


class StockQuant(models.Model):
    _inherit = 'stock.quant'

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self._fky_mark_dirty(records)
        return records

    def write(self, vals):
        res = super().write(vals)
        if {'quantity', 'product_id', 'location_id'} & set(vals):
            self._fky_mark_dirty(self)
        return res

    def unlink(self):
        products = self.mapped('product_id').ids
        locations = self.mapped('location_id').ids
        res = super().unlink()
        self._fky_mark_dirty_ids(products, locations)
        return res

    # ------------------------------------------------------------------ #
    # Dirty tracking
    # ------------------------------------------------------------------ #

    def _fky_mark_dirty(self, records):
        """Accumulate the (product, location) pairs of ``records``."""
        if not records:
            return
        self._fky_mark_dirty_ids(
            records.mapped('product_id').ids, records.mapped('location_id').ids)

    @api.model
    def _fky_mark_dirty_ids(self, product_ids, location_ids):
        """Add the cross-product of the given ids to the thread-local set.

        Marking only records the intent — no queries run here. The actual
        recompute happens once in :meth:`_fky_flush_dirty`.
        """
        if not product_ids or not location_ids:
            return
        dirty = getattr(_FKY_DIRTY, 'pairs', None)
        if dirty is None:
            dirty = _FKY_DIRTY.pairs = set()
        dirty.update((pid, lid) for pid in product_ids for lid in location_ids)

    @api.model
    def _fky_flush_dirty(self):
        """Recompute + persist the rules for every dirty pair, then clear.

        One ``read_group`` for all pairs and one batched write per value set,
        so a 500-row import costs a single recompute instead of 500.

        Wrapped defensively: ``stock.quant`` is core, and a dashboard refresh
        must never abort the inventory operation that triggered it.
        """
        dirty = getattr(_FKY_DIRTY, 'pairs', None)
        if not dirty:
            return
        # Snapshot + clear BEFORE recomputing so re-entrant quant writes
        # during the refresh cannot pile onto an already-flushing set.
        pairs = set(dirty)
        dirty.clear()
        try:
            self.env['fky.stock.rule'].sudo()._refresh_for_pairs(pairs)
        except Exception:
            # Never propagate; the nightly cron reconciles any drift.
            pass

    def _invalidate_cache(self):
        """Flush pending rule refreshes before the request cache is dropped.

        Odoo drops the environment cache at the end of the request (and on
        commit/rollback); flushing here guarantees the dashboard figures are
        persisted even when no further quant write follows in the request.
        """
        try:
            self._fky_flush_dirty()
        except Exception:
            pass
        return super()._invalidate_cache()
