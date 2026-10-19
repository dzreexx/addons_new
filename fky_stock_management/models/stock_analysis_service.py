# -*- coding: utf-8 -*-
from datetime import datetime, time

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models


class StockAnalysisService(models.AbstractModel):
    """Shared engine for purchase-overview metrics.

    Both the analysis wizard and the stock-rule smart preview delegate to this
    single service so the numbers can never drift.  Every optional data source
    is resolved through ``env.get`` so the module degrades gracefully when the
    sale or purchase modules are not installed.
    """

    _name = 'fky.stock.analysis.service'
    _description = 'Shared Stock Purchase Analysis Service'

    # ------------------------------------------------------------------ #
    # Optional-model detection (graceful degradation)
    # ------------------------------------------------------------------ #

    def _sale_available(self):
        """Sale-linked moves exist only when the sale suite is installed."""
        return 'sale_line_id' in self.env['stock.move']._fields

    # ------------------------------------------------------------------ #
    # BOM / raw material helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def _name_matches_raw(cls, finished_name, comp_name):
        """A component matches when its name is the finished product's name
        with an ``M-`` prefix (case/space insensitive)."""
        target = ' '.join((finished_name or '').split()).lower()
        if not target:
            return False
        comp = ' '.join((comp_name or '').split()).lower()
        return comp == 'm-' + target

    def _resolve_raw_material(self, parent, comps):
        """Return the bahan baku set a finished product *should* have.

        Whatever the component/BOM count is, only components whose name is
        the finished product's name prefixed with ``M-`` qualify
        (user decision 2026-08-31).
        """
        return comps.filtered(
            lambda t: self._name_matches_raw(parent.name, t.name))

    def _fill_raw_from_boms(self, boms):
        """Sync ``raw_material_ids`` with the auto-fill rules for saved BoMs.

        Auto rule is unchanged: only ``M-`` + finished name qualifies.
        Manual values (non-``M-`` match, e.g. ``M-Y`` for ``FG X`` or any
        other mat added by the user in the same ``Bahan Baku`` field) are
        merged and never wiped: an auto-managed record keeps
        ``expected_auto UNION manual_keep`` on every BoM save, and a manual
        record gets missing auto items added without removing its own.
        Stale auto items (still ``M-``-matching but gone from the BoMs)
        are removed like before. Returns the templates that were written.
        """
        Bom = self.env['mrp.bom']
        written = self.env['product.template'].browse()
        for parent in boms.mapped('product_tmpl_id'):
            if not parent:
                continue
            # union of components across ALL of the parent's active normal
            # BoMs — the caller may have saved only one of several BoMs
            parent_boms = Bom.search([
                ('product_tmpl_id', '=', parent.id),
                ('type', '=', 'normal'),
                ('active', '=', True),
            ])
            comps = parent_boms.mapped('bom_line_ids.product_tmpl_id').filtered(
                lambda t: t and t != parent)
            expected = self._resolve_raw_material(parent, comps)
            existing = parent.raw_material_ids
            if parent.raw_material_auto_filled:
                # Keep manual additions (anything that is NOT an M-match
                # for the current parent name). Stale auto items (still
                # M-matching but no longer expected) are dropped.
                manual_keep = existing.filtered(
                    lambda t: t.id not in expected.ids
                    and not self._name_matches_raw(parent.name, t.name))
                merged = expected | manual_keep
                if set(existing.ids) != set(merged.ids):
                    parent.with_context(fky_autofill=True).write({
                        'raw_material_ids': [(6, 0, merged.ids)],
                        'raw_material_auto_filled': bool(expected),
                    })
                    written |= parent
            elif existing or expected:
                # Manual record: never wipe manual, but still drop stale
                # auto (M-matching but no longer expected) and add missing
                # auto (new M- appearing after a manual edit).
                stale = existing.filtered(
                    lambda t: t.id not in expected.ids
                    and self._name_matches_raw(parent.name, t.name))
                missing = expected.filtered(
                    lambda t: t.id not in existing.ids)
                if stale or missing:
                    merged = (existing - stale) | missing
                    parent.with_context(fky_autofill=True).write({
                        'raw_material_ids': [(6, 0, merged.ids)],
                        'raw_material_auto_filled': bool(expected & merged),
                    })
                    written |= parent
        return written

    def _auto_fill_raw_materials(self):
        """Prefill bahan baku for every product matching the auto-fill rules."""
        Bom = self.env.get('mrp.bom')
        if Bom is None:
            return self.env['product.template'].browse()
        boms = Bom.search([
            ('type', '=', 'normal'),
            ('active', '=', True),
        ], order='sequence, id')
        return self._fill_raw_from_boms(boms)

    def _raw_ratios_batch(self, finished_tmpls):
        """Return ``{finished_tmpl_id: {raw_tmpl_id: ratio}}``.

        The ratio is ``mat_qty / bom_qty`` taken from the first BOM line
        (BOM order ``sequence, id``) that lists the material; 1:1 fallback
        otherwise. One single ``mrp.bom`` search covers every template —
        there is no per-product search anywhere in this module.
        """
        result = {
            t.id: {raw.id: 1.0 for raw in t.raw_material_ids}
            for t in finished_tmpls
        }
        if not finished_tmpls:
            return result
        Bom = self.env.get('mrp.bom')
        if Bom is None:
            return result
        boms = Bom.search([
            ('product_tmpl_id', 'in', finished_tmpls.ids),
            ('type', '=', 'normal'),
            ('active', '=', True),
        ], order='sequence, id')
        seen = set()
        for line in boms.mapped('bom_line_ids'):
            fin_id = line.bom_id.product_tmpl_id.id
            ratios = result.get(fin_id)
            if not ratios:
                continue
            raw_id = line.product_tmpl_id.id
            if raw_id in ratios and (fin_id, raw_id) not in seen:
                seen.add((fin_id, raw_id))
                bom_qty = line.bom_id.product_qty or 1.0
                ratios[raw_id] = (line.product_qty or 1.0) / bom_qty
        return result

    # ------------------------------------------------------------------ #
    # Open-move domains (ghost-move exclusion)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _no_ghost_domain():
        """Domain terms excluding ghost moves under cancelled pickings.

        A cancelled receipt/sale (``stock.picking`` state ``cancel``) sometimes
        keeps its moves in an open state (``assigned``/``confirmed``/...)
        instead of cancelling them (e.g. PO P00246 / GDADR/IN/00061). Those
        ghost moves must not inflate incoming / sales-order figures. Moves
        without any picking are kept (first branch).
        """
        return ['|', ('picking_id', '=', False),
                ('picking_id.state', '!=', 'cancel')]

    # ------------------------------------------------------------------ #
    # Components-popup stock (toko+gudang on-hand + incoming)
    # ------------------------------------------------------------------ #

    def _branch_locations(self, company):
        """Return ``(toko_loc, gudang_loc)`` for the company's branch.

        Resolved directly by company (``TK*`` = toko, ``GD*`` = gudang,
        ``*/Stock``), NOT via the shared on-hand config — so every company
        gets its own pair. Empty recordsets when not found.
        """
        Location = self.env['stock.location']
        if not company:
            return Location.browse(), Location.browse()
        locs = Location.search([
            ('company_id', '=', company.id),
            ('usage', '=', 'internal'),
        ], order='complete_name, id')
        toko = Location.browse()
        gudang = Location.browse()
        for loc in locs:
            cname = (loc.complete_name or '').upper()
            if not cname.endswith('/STOCK'):
                continue
            if cname.startswith('TK') and not toko:
                toko = loc
            elif cname.startswith('GD') and not gudang:
                gudang = loc
        return toko, gudang

    def _component_stock(self, company, products):
        """Return ``{product_id: {'onhand_qty': x, 'incoming_qty': y}}``.

        On-hand counts ONLY the branch toko+gudang locations (other
        internal locations like Karantina/Transit are excluded by design).
        Incoming is company-wide with the same definition as the analysis
        (waiting/confirmed/assigned/partially_available, ghost moves under
        cancelled pickings excluded).
        """
        res = {
            p.id: {'onhand_qty': 0.0, 'incoming_qty': 0.0} for p in products
        }
        if not products or not company:
            return res
        products = products.with_company(company)
        toko, gudang = self._branch_locations(company)
        branch_ids = (toko | gudang).ids
        if branch_ids:
            groups = self.env['stock.quant'].read_group(
                [('product_id', 'in', products.ids),
                 ('location_id', 'in', branch_ids)],
                ['product_id', 'quantity:sum'], ['product_id'], lazy=False,
            )
            for pid, qty in self._grouped_quantities(
                    groups, 'quantity').items():
                res[pid]['onhand_qty'] = qty
        _qd, move_in_domain, _od = self._stock_domains(products, company)
        moves = self.env['stock.move'].read_group(
            [('product_id', 'in', products.ids),
             ('state', 'in', ('waiting', 'confirmed', 'assigned',
                              'partially_available')),
             ('scrapped', '=', False),
             ('company_id', '=', company.id)]
            + self._no_ghost_domain() + move_in_domain,
            ['product_id', 'product_qty:sum'], ['product_id'], lazy=False,
        )
        for pid, qty in self._grouped_quantities(moves, 'product_qty').items():
            res[pid]['incoming_qty'] = qty
        return res

    # ------------------------------------------------------------------ #
    # Grouped-read helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _grouped_quantities(groups, field):
        return {
            group['product_id'][0]: group.get(field, 0.0)
            for group in groups if group.get('product_id')
        }

    # ------------------------------------------------------------------ #
    # Isi Per Dus rounding
    # ------------------------------------------------------------------ #

    @staticmethod
    def round_to_isi_perdus(qty, isi_perdus):
        """Round ``qty`` up to a whole number of dus (Excel formula).

        Mirrors ``=ROUND(qty / isi_perdus; 0) * isi_perdus`` where Excel's
        banker-free ROUND means "nearest whole dus". A value already on a
        dus boundary is unchanged, so re-running never inflates the number
        (the old Excel risk of an exact .5 going one dus up forever).

        Returns a float, or ``0.0`` for a non-positive ``qty``. A missing /
        zero ``isi_perdus`` means the product has no dus packing, so the
        quantity is returned untouched (no division by zero).
        """
        if not qty or qty <= 0.0:
            return 0.0
        if not isi_perdus or isi_perdus <= 0.0:
            return float(qty)
        # Half-up on the dus count, evaluated on integers to stay immune to
        # binary-float noise (e.g. 250/20 -> 12.499999...).
        dus = int((qty * 2.0 + isi_perdus) // (isi_perdus * 2.0))
        return dus * float(isi_perdus)

    def _isi_perdus_map(self, products):
        """Return ``{product_id: isi_perdus}`` from the product master.

        Tolerates the AOS module being absent (field missing)."""
        if not products:
            return {}
        if 'isi_perdus' not in products._fields:
            return {}
        return {p.id: (p.isi_perdus or 0.0) for p in products}

    def _apply_isi_perdus(self, products, metrics):
        """Round the plannable quantity to whole dus, in place.

        ``recommended_purchase_qty`` becomes the rounded figure — this IS the
        planning quantity now, there is no separate raw value to keep.

        ``target_stock_qty`` is deliberately NOT rounded: ``Ketahanan Bulan``
        derives from the real target, and rounding it would silently change
        the months-of-coverage figures shown next to the planning column.
        """
        isi_map = self._isi_perdus_map(products)
        if not isi_map:
            return metrics
        for pid, values in metrics.items():
            values['recommended_purchase_qty'] = self.round_to_isi_perdus(
                values.get('recommended_purchase_qty', 0.0) or 0.0,
                isi_map.get(pid, 0.0))
        return metrics

    def _stock_domains(self, products, company):
        """Use stock's standard all-internal-location logic for this company."""
        return products.with_context(
            allowed_company_ids=[company.id],
        )._get_domain_locations()

    def _on_hand_domain(self, company):
        """Extra domain terms restricting on-hand to configured locations.

        When ``fky.stock.config.on_hand_location_ids`` is set, only those
        locations count as current stock (for both finished and raw material
        reads). The configured set is intersected with the company's own
        internal locations, so per-company separation is preserved even though
        the config record is shared. Returns ``[]`` when unset (old behaviour).
        """
        config = self.env['fky.stock.config']._get()
        if not config.on_hand_location_ids:
            return []
        return [('location_id', 'in', config.on_hand_location_ids.ids)]

    def _preferred_vendors(self, products, company):
        """Get one applicable priority vendor per product without product loops."""
        today = fields.Date.context_today(self)
        vendors = {}
        if not products:
            return vendors
        suppliers = self.env['product.supplierinfo'].search([
            ('product_tmpl_id', 'in', products.mapped('product_tmpl_id').ids),
            '|', ('company_id', '=', False), ('company_id', '=', company.id),
            '|', ('date_start', '=', False), ('date_start', '<=', today),
            '|', ('date_end', '=', False), ('date_end', '>=', today),
        ], order='sequence, id')
        product_by_template = {}
        for product in products:
            product_by_template.setdefault(product.product_tmpl_id.id, []).append(product.id)
        for supplier in suppliers:
            product_ids = [supplier.product_id.id] if supplier.product_id else product_by_template.get(supplier.product_tmpl_id.id, [])
            for product_id in product_ids:
                vendors.setdefault(product_id, supplier.name.id)
        return vendors

    # ------------------------------------------------------------------ #
    # Main computation
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Saved MoQ per product (remembered across reports)
    # ------------------------------------------------------------------ #

    @api.model
    def get_saved_moq(self, products, company):
        """Return ``{product_id: moq}`` from remembered manual MoQs."""
        if not products or not company:
            return {}
        rules = self.env['fky.stock.moq.rule'].search([
            ('product_id', 'in', products.ids),
            ('company_id', '=', company.id),
            ('active', '=', True),
        ])
        return {rule.product_id.id: rule.moq_qty for rule in rules}

    @api.model
    def default_moq_batch(self, products, vendor_map, company):
        """Batched :meth:`default_moq` in ONE pass — no per-product queries.

        Priority per product: remembered manual MoQ > seller ``min_qty`` >
        0. ``vendor_map`` is ``{product_id: vendor_id or False}`` (as produced
        by :meth:`_preferred_vendors` / ``analyze``). Seller minimums are
        read with a single ``product.supplierinfo`` search for all templates,
        instead of one ``_select_seller`` call per product.
        """
        result = {p.id: 0.0 for p in products}
        if not products:
            return result
        saved = self.get_saved_moq(products, company) if company else {}
        for product_id, moq in saved.items():
            if product_id in result:
                result[product_id] = moq
        # Only products without a remembered MoQ need the seller lookup.
        pending = [p for p in products if p.id not in saved]
        if not pending:
            return result
        today = fields.Date.context_today(self)
        sellers = self.env['product.supplierinfo'].search([
            ('product_tmpl_id', 'in', [p.product_tmpl_id.id for p in pending
                                       if p.product_tmpl_id]),
            '|', ('company_id', '=', False),
            ('company_id', '=', company.id) if company else ('company_id', '=', False),
            '|', ('date_start', '=', False), ('date_start', '<=', today),
            '|', ('date_end', '=', False), ('date_end', '>=', today),
        ], order='sequence, id')
        # Best (lowest sequence) seller per product template; a seller bound
        # to a specific product variant wins over the template-level one.
        best_by_variant = {}
        best_by_template = {}
        for seller in sellers:
            tmpl_id = seller.product_tmpl_id.id
            if seller.product_id:
                best_by_variant.setdefault(seller.product_id.id, seller)
            elif tmpl_id:
                best_by_template.setdefault(tmpl_id, seller)
        for product in pending:
            seller = (best_by_variant.get(product.id)
                      or best_by_template.get(product.product_tmpl_id.id))
            if seller and seller.min_qty:
                result[product.id] = seller.min_qty
        return result

    @api.model
    def get_first_bom(self, product):
        """Return the first active normal BOM of a product (or empty).

        Same semantics as the dashboard Components viewer. Safe when the
        mrp module is not installed.
        """
        Bom = self.env.get('mrp.bom')
        if Bom is None or not product or not product.product_tmpl_id:
            return Bom
        return Bom.search([
            ('product_tmpl_id', '=', product.product_tmpl_id.id),
            ('type', '=', 'normal'),
            ('active', '=', True),
        ], order='sequence, id', limit=1)

    def average_sales(self, company, products, months):
        """Return ``{product_id: average monthly sales}`` for the last ``months``.

        Lightweight variant of :meth:`analyze` that only runs the historical
        sales read (one grouped query) and skips the on-hand / incoming /
        sales-order reads. Used by the dashboard's Best Seller column so the
        page can refresh live without paying for the full analysis.
        """
        if not months or months <= 0 or not products or not self._sale_available():
            return {}
        products = products.with_company(company)
        today = fields.Date.context_today(self)
        current_month_start = today.replace(day=1)
        history_start = current_month_start - relativedelta(months=months)
        history_end = current_month_start

        _, _, move_out_domain = self._stock_domains(products, company)
        company_domain = [('company_id', '=', company.id)]
        history_groups = self.env['stock.move'].read_group(
            [('product_id', 'in', products.ids), ('state', '=', 'done'),
             ('scrapped', '=', False), ('location_dest_id.usage', '=', 'customer'),
             ('date', '>=', datetime.combine(history_start, time.min)),
             ('date', '<', datetime.combine(history_end, time.min))]
            + company_domain + move_out_domain,
            ['product_id', 'product_qty:sum'], ['product_id'], lazy=False,
        )
        grouped = self._grouped_quantities(history_groups, 'product_qty')
        return {pid: qty / months for pid, qty in grouped.items()}

    def analyze(self, company, products, average_months, stock_coverage_months,
                combine='sum'):
        """Compute per-product coverage measures for a set of products.

        :return: dict {product_id: {field: value}} where fields mirror the
            analysis-line model (on_hand_qty, incoming_qty, sales_order_qty,
            average_monthly_sales, target_stock_qty, recommended_purchase_qty,
            months_of_stock, future_months_of_stock, vendor_id).

        :param combine: ``'sum'`` (default, existing behaviour for the
            dashboard and other menus) or ``'min'`` (complementary
            materials: 1 FG needs ALL mats, e.g. FG X needs M-X + M-Y,
            so convertible stock is ``min(mat/ratio)``). Preview analysis
            and saved reports pass ``'min'``; BOM ratios are read from the
            BOM in both modes (``mat_qty / bom_qty``, 1:1 fallback).

        Raw-material incoming is mirrored onto the finished product and
        summed with any incoming the FG already has — but only in ``'min'``
        mode (Preview + saved reports); the dashboard keeps the product's
        own physical incoming. Single-mat FGs mirror 1:1 (no ratio);
        multi-mat FGs mirror the bottleneck (``min(incoming/ratio)``),
        same as on-hand.

        When the sale modules are absent the sales figures are simply zero and
        no error is raised; consumers just get a stock-only recommendation.
        """
        products = products.with_company(company)
        if not products:
            return {}

        product_ids = products.ids
        quant_domain, move_in_domain, move_out_domain = self._stock_domains(products, company)
        company_domain = [('company_id', '=', company.id)]

        history_days = None
        today = fields.Date.context_today(self)
        if average_months and average_months > 0:
            current_month_start = today.replace(day=1)
            history_start = current_month_start - relativedelta(months=average_months)
            history_end = current_month_start
            history_days = (datetime.combine(history_start, time.min), datetime.combine(history_end, time.min))

        quant_groups = self.env['stock.quant'].read_group(
            [('product_id', 'in', product_ids)] + quant_domain + self._on_hand_domain(company),
            ['product_id', 'quantity:sum'], ['product_id'], lazy=False,
        )
        on_hand = self._grouped_quantities(quant_groups, 'quantity')

        raw_included = {}
        if self.env.get('mrp.bom') is not None:
            raw_ids = []
            ratio_map = self._raw_ratios_batch(products.product_tmpl_id)
            for product in products:
                ratios = ratio_map.get(product.product_tmpl_id.id, {})
                for raw_tmpl in product.product_tmpl_id.raw_material_ids:
                    comp = raw_tmpl.product_variant_ids[:1]
                    # Only skip self (own variant). Do NOT skip raws that
                    # are also in the analysis (e.g. full analysis of all
                    # products, like FG MISBH06 + its 3 M- mats): the FG
                    # line must still show convertible stock even when the
                    # mat has its own line. Old `not in product_ids` check
                    # silently zeroed the extra in that case.
                    if comp and comp.id != product.id:
                        raw_included.setdefault(product.id, []).append(
                            (comp.id, ratios.get(raw_tmpl.id, 1.0)))
                        raw_ids.append(comp.id)
            if raw_ids:
                raw_groups = self.env['stock.quant'].read_group(
                    [('product_id', 'in', raw_ids)] + quant_domain + self._on_hand_domain(company),
                    ['product_id', 'quantity:sum'], ['product_id'], lazy=False,
                )
                raw_on_hand = self._grouped_quantities(raw_groups, 'quantity')
                use_min = (combine == 'min')
                for pid, comp_ratios in raw_included.items():
                    convertible = [
                        raw_on_hand.get(rid, 0.0) / ratio
                        for rid, ratio in comp_ratios if ratio
                    ]
                    if not convertible:
                        continue
                    if use_min:
                        # Complementary mats (FG X needs M-X AND M-Y):
                        # bottleneck determines convertible stock.
                        # Missing mat (0.0) correctly yields 0 extra.
                        extra = min(convertible)
                    else:
                        extra = sum(convertible)
                    on_hand[pid] = on_hand.get(pid, 0.0) + extra

        # Equivalent finished products (Produk Setara, e.g. A combined
        # with B): plain physical on-hand added 1:1 with NO BOM ratio and
        # NO explosion of the equivalent's own mats (single level only).
        # Only for Preview + Saved Reports (combine='min' callers); the
        # dashboard and other menus keep real physical quantity.
        if combine == 'min':
            equiv_included = {}
            equiv_ids = []
            for product in products:
                for eq_tmpl in product.product_tmpl_id.equivalent_product_ids:
                    if not eq_tmpl or eq_tmpl.id == product.product_tmpl_id.id:
                        continue
                    comp = eq_tmpl.product_variant_ids[:1]
                    if comp and comp.id != product.id:
                        equiv_included.setdefault(product.id, []).append(comp.id)
                        equiv_ids.append(comp.id)
            if equiv_ids:
                equiv_groups = self.env['stock.quant'].read_group(
                    [('product_id', 'in', list(set(equiv_ids)))] + quant_domain + self._on_hand_domain(company),
                    ['product_id', 'quantity:sum'], ['product_id'], lazy=False,
                )
                equiv_on_hand = self._grouped_quantities(equiv_groups, 'quantity')
                for pid, cids in equiv_included.items():
                    on_hand[pid] = on_hand.get(pid, 0.0) + sum(
                        equiv_on_hand.get(cid, 0.0) for cid in cids)

        # Component breakdown per product (first active normal BOM), batched:
        # one BOM search for all templates (same semantics as the dashboard
        # Components viewer). Used to show buyers what each unit needs.
        components = {}
        Bom = self.env.get('mrp.bom')
        if Bom is not None:
            tmpl_ids = list({
                p.product_tmpl_id.id for p in products if p.product_tmpl_id
            })
            first_bom = {}
            if tmpl_ids:
                for bom in Bom.search([
                    ('product_tmpl_id', 'in', tmpl_ids),
                    ('type', '=', 'normal'),
                    ('active', '=', True),
                ], order='sequence, id'):
                    first_bom.setdefault(bom.product_tmpl_id.id, bom)
            for product in products:
                bom = first_bom.get(
                    product.product_tmpl_id.id) if product.product_tmpl_id else None
                if bom and bom.bom_line_ids:
                    bom_qty = bom.product_qty or 1.0
                    components[product.id] = [{
                        'code': line.product_id.default_code or '',
                        'name': line.product_id.display_name,
                        'qty_per_unit': (line.product_qty or 0.0) / bom_qty,
                        'uom': line.product_uom_id.name,
                    } for line in bom.bom_line_ids if line.product_id]

        incoming_groups = self.env['stock.move'].read_group(
            [('product_id', 'in', product_ids), ('state', 'in', ('waiting', 'confirmed', 'assigned', 'partially_available')),
             ('scrapped', '=', False)] + self._no_ghost_domain() + company_domain + move_in_domain,
            ['product_id', 'product_qty:sum'], ['product_id'], lazy=False,
        )
        incoming = self._grouped_quantities(incoming_groups, 'product_qty')

        # Raw-material incoming mirrored onto finished products: what the
        # mats show as incoming also shows in the FG, summed with any
        # incoming the FG already has (single-mat 1:1, multi-mat
        # bottleneck). Only for Preview + Saved Reports (combine='min'
        # menus keep the product's own physical incoming. Ghost moves under
        # cancelled pickings are excluded (same domain as the FG read).
        if combine == 'min' and raw_included:
            mat_ids = sorted({
                rid for pairs in raw_included.values() for rid, _ratio in pairs
            })
            mat_in_groups = self.env['stock.move'].read_group(
                [('product_id', 'in', mat_ids), ('state', 'in', ('waiting', 'confirmed', 'assigned', 'partially_available')),
                 ('scrapped', '=', False)] + self._no_ghost_domain() + company_domain + move_in_domain,
                ['product_id', 'product_qty:sum'], ['product_id'], lazy=False,
            )
            mat_incoming = self._grouped_quantities(mat_in_groups, 'product_qty')
            for pid, comp_ratios in raw_included.items():
                if len(comp_ratios) > 1:
                    # Multi-mat FG: bottleneck in FG units
                    # (min(incoming/ratio)), same logic as on-hand. A
                    # missing mat (0.0) correctly yields 0 extra.
                    convertible = [
                        mat_incoming.get(rid, 0.0) / ratio
                        for rid, ratio in comp_ratios if ratio
                    ]
                    extra = min(convertible) if convertible else 0.0
                else:
                    # Single-mat FG: keep the 1:1 mirror (no ratio,
                    # no behavior change).
                    extra = sum(
                        mat_incoming.get(rid, 0.0) for rid, _ratio in comp_ratios
                    )
                if extra:
                    incoming[pid] = incoming.get(pid, 0.0) + extra

        sales_order = {}
        historical_sales = {}
        if self._sale_available():
            sales_groups = self.env['stock.move'].read_group(
                [('product_id', 'in', product_ids), ('sale_line_id', '!=', False),
                 ('sale_line_id.order_id.state', 'in', ('sale', 'done')),
                 ('state', 'in', ('waiting', 'confirmed', 'assigned', 'partially_available')),
                 ('scrapped', '=', False), ('location_dest_id.usage', '=', 'customer')]
                + self._no_ghost_domain() + company_domain + move_out_domain,
                ['product_id', 'product_qty:sum'], ['product_id'], lazy=False,
            )
            sales_order = self._grouped_quantities(sales_groups, 'product_qty')
            if history_days:
                history_groups = self.env['stock.move'].read_group(
                    [('product_id', 'in', product_ids), ('state', '=', 'done'), ('scrapped', '=', False),
                     ('location_dest_id.usage', '=', 'customer'),
                     ('date', '>=', history_days[0]),
                     ('date', '<', history_days[1])]
                    + company_domain + move_out_domain,
                    ['product_id', 'product_qty:sum'], ['product_id'], lazy=False,
                )
                historical_sales = self._grouped_quantities(history_groups, 'product_qty')

        vendors = self._preferred_vendors(products, company)

        periods = average_months if average_months and average_months > 0 else 1
        values = {}
        for product in products:
            product_on_hand = on_hand.get(product.id, 0.0)
            product_incoming = incoming.get(product.id, 0.0)
            product_so = sales_order.get(product.id, 0.0)
            average_sales = historical_sales.get(product.id, 0.0) / periods
            future_stock = product_on_hand + product_incoming - product_so
            months_of_stock = (product_on_hand - product_so) / average_sales if average_sales else 0.0
            future_months = future_stock / average_sales if average_sales else 0.0
            target_stock = average_sales * (stock_coverage_months or 0)
            recommended_purchase = max(0.0, target_stock - future_stock)
            values[product.id] = {
                'vendor_id': vendors.get(product.id),
                'on_hand_qty': product_on_hand,
                'incoming_qty': product_incoming,
                'sales_order_qty': product_so,
                'average_monthly_sales': average_sales,
                'target_stock_qty': target_stock,
                'recommended_purchase_qty': recommended_purchase,
                'months_of_stock': months_of_stock,
                'future_months_of_stock': future_months,
                'components': components.get(product.id, []),
            }
        # Planning Pembelian is rounded to whole dus (isi perdus) for every
        # consumer: dashboard, wizard preview and saved reports.
        return self._apply_isi_perdus(products, values)