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
    def _normalized_code(cls, code):
        """Case/space-normalised product code used by the M- matcher."""
        return (code or '').strip().lower()

    @classmethod
    def _raw_code_key(cls, code):
        """Return the finished code encoded by an ``M-`` raw material code."""
        normalized = cls._normalized_code(code)
        if not normalized.startswith('m-'):
            return None
        key = normalized[2:].strip()
        return key or None

    @classmethod
    def _code_matches_raw(cls, finished_code, comp_code):
        """A component matches when its code is exactly ``M-`` + FG code."""
        target = cls._normalized_code(finished_code)
        if not target:
            return False
        return cls._raw_code_key(comp_code) == target

    @classmethod
    def _is_raw_product(cls, product):
        return bool(cls._raw_code_key(product.default_code))

    def _matches_raw(self, parent, raw):
        """The auto rule: exact ``M-`` + FG code only."""
        return self._code_matches_raw(parent.default_code, raw.default_code)

    def _raw_code_map(self):
        """Map ``finished code`` -> raw templates whose code is ``M-`` + code.

        This is the only auto-fill rule: the raw material code must be exactly
        ``M-`` + the finished product code (for example ``M-BSKSH05`` for
        ``BSKSH05``).
        """
        raws = self.env['product.template'].search([
            ('default_code', '=ilike', 'M-%'),
        ])
        mapping = {}
        for raw in raws:
            key = self._raw_code_key(raw.default_code)
            if key:
                mapping[key] = mapping.get(
                    key, self.env['product.template']) | raw
        return mapping

    def _resolve_raw_material(self, parent, comps, code_map=None):
        """Return the bahan baku set a finished product *should* have.

        The exact ``M-`` + code rule is the only rule. BoM components are
        checked with the same rule.
        """
        empty = self.env['product.template']
        expected = comps.filtered(
            lambda t: self._code_matches_raw(
                parent.default_code, t.default_code))
        if code_map:
            expected |= code_map.get(
                self._normalized_code(parent.default_code), empty)
        return expected.filtered(lambda t: t and t != parent)

    def _fill_raw_for_parents(self, parents, code_map=None,
                              drop_ids=None, drop_by_parent=None):
        """Sync ``raw_material_ids`` for the given finished products.

        Auto rule: a component qualifies when its code is exactly ``M-`` +
        the finished code. BoM components and standalone products are both
        considered, so products without a BoM are covered too. Manual values
        (non-matching) are merged and never wiped. ``drop_ids`` /
        ``drop_by_parent`` remove old auto values after a recode (manual links
        are left untouched).
        """
        parents = parents.exists()
        if not parents:
            return self.env['product.template'].browse()
        if code_map is None:
            code_map = self._raw_code_map()
        Bom = self.env.get('mrp.bom')
        comps_by_parent = {}
        if Bom is not None:
            boms = Bom.search([
                ('product_tmpl_id', 'in', parents.ids),
                ('type', '=', 'normal'),
                ('active', '=', True),
            ], order='sequence, id')
            for line in boms.mapped('bom_line_ids'):
                parent = line.bom_id.product_tmpl_id
                comp = line.product_tmpl_id
                if parent and comp:
                    comps_by_parent[parent.id] = comps_by_parent.get(
                        parent.id, self.env['product.template']) | comp
        written = self.env['product.template'].browse()
        empty = self.env['product.template']
        drop_by_parent = drop_by_parent or {}
        for parent in parents:
            comps = comps_by_parent.get(parent.id, empty)
            expected = self._resolve_raw_material(parent, comps, code_map)
            db_existing = parent.raw_material_ids
            existing = db_existing
            parent_drop_ids = set(drop_ids or ())
            parent_drop_ids.update(drop_by_parent.get(parent.id, ()))
            if parent_drop_ids and parent.raw_material_auto_filled:
                existing = existing - self.env['product.template'].browse(
                    parent_drop_ids)
            if parent.raw_material_auto_filled:
                # Keep manual additions (anything that is NOT an M-match
                # for the current parent code). Stale auto items (still
                # M-matching but no longer expected) are dropped.
                manual_keep = existing.filtered(
                    lambda t: t.id not in expected.ids
                    and not self._matches_raw(parent, t))
                merged = expected | manual_keep
                new_flag = bool(expected)
                if (set(db_existing.ids) != set(merged.ids)
                        or parent.raw_material_auto_filled != new_flag):
                    parent.with_context(fky_autofill=True).write({
                        'raw_material_ids': [(6, 0, merged.ids)],
                        'raw_material_auto_filled': new_flag,
                    })
                    written |= parent
            elif existing or expected:
                # Manual record: never wipe manual, but still drop stale
                # auto (M-matching but no longer expected) and add missing
                # auto (new M- appearing after a manual edit).
                stale = existing.filtered(
                    lambda t: t.id not in expected.ids
                    and self._matches_raw(parent, t))
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

    def _fill_raw_from_boms(self, boms):
        """Sync bahan baku for the parents of the given BoMs."""
        return self._fill_raw_for_parents(boms.mapped('product_tmpl_id'))

    def _find_templates_by_code_key(self, key):
        """Find finished templates whose normalised code equals ``key``."""
        if not key:
            return self.env['product.template'].browse()
        candidates = self.env['product.template'].search([
            ('default_code', '=ilike', key),
        ])
        return candidates.filtered(
            lambda t: self._normalized_code(t.default_code) == key)

    def _sync_raw_materials_for_products(self, products, old_codes=None):
        """Targeted sync after a product is created or recoded.

        Covers both directions (FG -> its M- raw; M- raw -> its FG) and BoM
        parents that use the product as a component. The only rule is exact
        ``M-`` + FG code.
        """
        products = products.exists()
        if not products:
            return self.env['product.template'].browse()
        code_map = self._raw_code_map()
        parents = self.env['product.template'].browse()
        drop_ids = set()
        drop_by_parent = {}
        for product in products:
            raw_code_key = self._raw_code_key(product.default_code)
            if raw_code_key:
                parents |= self._find_templates_by_code_key(raw_code_key)
            else:
                parents |= product
            old_code = (old_codes or {}).get(product.id)
            if old_code:
                old_raw_code_key = self._raw_code_key(old_code)
                if old_raw_code_key:
                    drop_ids.add(product.id)
                    parents |= self._find_templates_by_code_key(
                        old_raw_code_key)
                else:
                    old_raws = code_map.get(
                        self._normalized_code(old_code),
                        self.env['product.template'])
                    if old_raws:
                        drop_by_parent.setdefault(product.id, set()).update(
                            old_raws.ids)
                    parents |= product
            Bom = self.env.get('mrp.bom')
            if Bom is not None:
                lines = self.env['mrp.bom.line'].search([
                    ('product_tmpl_id', '=', product.id),
                ])
                parents |= lines.mapped('bom_id.product_tmpl_id')
        return self._fill_raw_for_parents(
            parents, code_map=code_map, drop_ids=drop_ids,
            drop_by_parent=drop_by_parent)

    def _auto_fill_raw_materials(self):
        """Prefill bahan baku for every product matching the auto-fill rules."""
        code_map = self._raw_code_map()
        parents = self.env['product.template'].browse()
        Bom = self.env.get('mrp.bom')
        if Bom is not None:
            boms = Bom.search([
                ('type', '=', 'normal'),
                ('active', '=', True),
            ], order='sequence, id')
            parents |= boms.mapped('product_tmpl_id')
        if code_map:
            products = self.env['product.template'].search([])
            parents |= products.filtered(
                lambda t: not self._is_raw_product(t)
                and self._normalized_code(t.default_code) in code_map)
        parents |= self.env['product.template'].search([
            ('raw_material_auto_filled', '=', True),
        ])
        return self._fill_raw_for_parents(parents, code_map=code_map)

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

    def _preferred_vendor_priorities(self, products, company):
        """Return ``{product_id: (sequence, vendor_id)}`` in one pass.

        The winning vendor is the applicable ``product.supplierinfo`` with the
        lowest sequence (ties by id), exactly the priority order used by
        :meth:`_preferred_vendors`.
        """
        today = fields.Date.context_today(self)
        choices = {}
        if not products:
            return choices
        suppliers = self.env['product.supplierinfo'].search([
            ('product_tmpl_id', 'in', products.mapped('product_tmpl_id').ids),
            '|', ('company_id', '=', False), ('company_id', '=', company.id),
            '|', ('date_start', '=', False), ('date_start', '<=', today),
            '|', ('date_end', '=', False), ('date_end', '>=', today),
        ], order='sequence, id')
        product_by_template = {}
        for product in products:
            product_by_template.setdefault(
                product.product_tmpl_id.id, []).append(product.id)
        for supplier in suppliers:
            sequence = supplier.sequence or 0
            product_ids = (
                [supplier.product_id.id] if supplier.product_id
                else product_by_template.get(supplier.product_tmpl_id.id, []))
            for product_id in product_ids:
                current = choices.get(product_id)
                if current is None or sequence < current[0]:
                    choices[product_id] = (sequence, supplier.name.id)
        return choices

    def _preferred_vendors(self, products, company):
        """Get one applicable priority vendor per product without product loops."""
        return {
            product_id: vendor_id
            for product_id, (_sequence, vendor_id)
            in self._preferred_vendor_priorities(products, company).items()
        }

    def _mirror_raw_material_vendors(self, products, vendors, company):
        """Fill a finished product's vendor from its linked bahan baku.

        Only used when the finished product itself has no applicable supplier:
        just like on-hand/incoming, the mats' vendor is mirrored onto the FG.
        When several mats have vendors, the one with the lowest supplierinfo
        sequence wins (same priority order as ``_preferred_vendors``).
        """
        product_raws = {}
        raw_tmpl_ids = set()
        for product in products:
            tmpl = product.product_tmpl_id
            raws = (
                tmpl.raw_material_ids if tmpl
                else self.env['product.template'])
            if raws:
                product_raws[product.id] = raws
                raw_tmpl_ids.update(raws.ids)
        if not raw_tmpl_ids:
            return vendors
        raw_products = self.env['product.product'].search([
            ('product_tmpl_id', 'in', list(raw_tmpl_ids)),
        ])
        raw_priorities = self._preferred_vendor_priorities(
            raw_products, company)
        tmpl_priority = {}
        for raw in raw_products:
            priority = raw_priorities.get(raw.id)
            if not priority:
                continue
            current = tmpl_priority.get(raw.product_tmpl_id.id)
            if current is None or priority[0] < current[0]:
                tmpl_priority[raw.product_tmpl_id.id] = priority
        for product_id, raws in product_raws.items():
            if vendors.get(product_id):
                continue
            best = None
            for raw in raws:
                priority = tmpl_priority.get(raw.id)
                if priority and (best is None or priority[0] < best[0]):
                    best = priority
            if best:
                vendors[product_id] = best[1]
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
        vendors = self._mirror_raw_material_vendors(products, vendors, company)

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