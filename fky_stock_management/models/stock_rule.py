# -*- coding: utf-8 -*-
from odoo import api, fields, models


class StockRule(models.Model):
    _name = 'fky.stock.rule'
    _description = 'Stock Alert & Purchase Rule'
    _rec_name = 'product_id'
    _order = 'is_favorite desc, qty_available asc'

    product_id = fields.Many2one(
        'product.product',
        string='Product',
        required=True,
    )

    location_id = fields.Many2one(
        'stock.location',
        string='Location',
        required=True,
        domain=[('complete_name', 'in', ['TKADR/Stock', 'GDADR/Stock', 'TKANA/Stock', 'GDANA/Stock'])],
    )

    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='location_id.company_id',
        store=True,
        readonly=True,
    )

    min_qty = fields.Float(
        string='Minimum Qty',
        required=True,
        default=1,
    )

    is_favorite = fields.Boolean(
        string='Favorite',
    )

    qty_available = fields.Float(
        string='Current Qty',
        compute='_compute_qty',
        store=True,
    )

    # Counterpart location per branch: a rule on a toko (TK...) location
    # surfaces the gudang (GD...) stock of the same branch, and vice versa.
    _COUNTERPART_MAP = {
        'TKADR/Stock': 'GDADR/Stock',
        'GDADR/Stock': 'TKADR/Stock',
        'TKANA/Stock': 'GDANA/Stock',
        'GDANA/Stock': 'TKANA/Stock',
    }

    other_location_id = fields.Many2one(
        'stock.location',
        string='Other Location',
        compute='_compute_other_stock',
        help='Lokasi pasangan: gudang untuk rule toko, toko untuk rule gudang.',
    )

    other_qty_available = fields.Float(
        string='Other Location On Hand',
        compute='_compute_other_stock',
        help='Stok fisik produk yang sama di lokasi pasangan '
             '(TKADR↔GDADR / TKANA↔GDANA), tanpa penjumlahan bahan baku.',
    )

    state = fields.Selection([
        ('ok', 'OK'),
        ('low', 'Low Stock'),
        ('urgent', 'Urgent'),
    ], string='Status', compute='_compute_qty', store=True)

    snooze_until = fields.Date(
        string='Snooze Until',
    )

    is_snoozed = fields.Boolean(
        string='Snoozed',
        compute='_compute_is_snoozed',
        search='_search_is_snoozed',
    )

    has_components = fields.Boolean(
        string='Has Components',
        compute='_compute_has_components',
        search='_search_has_components',
    )

    component_count = fields.Integer(
        string='Component Count',
        compute='_compute_has_components',
        store=True,
    )

    # ---- Purchase analysis preview (live, via shared service) ---- #
    incoming_qty = fields.Float(
        string='Incoming Qty',
        help='Total expected incoming quantity for this product/location.',
        compute='_compute_analysis_preview',
        readonly=True,
    )

    average_monthly_sales = fields.Float(
        string='Avg Monthly Sales',
        help='Average monthly sales computed from the company history.',
        compute='_compute_analysis_preview',
        readonly=True,
    )

    future_months_of_stock = fields.Float(
        string='Future Months of Stock',
        help='How many months the current stock plus incoming will last.',
        compute='_compute_analysis_preview',
        readonly=True,
    )

    recommended_purchase_qty = fields.Float(
        string='Recommended Buy',
        help='Quantity that should be bought to reach the target stock coverage.',
        compute='_compute_analysis_preview',
        readonly=True,
    )

    vendor_id = fields.Many2one(
        'res.partner',
        string='Vendor',
        help='Preferred vendor for this product.',
        compute='_compute_analysis_preview',
        readonly=True,
    )

    best_seller_average_sales = fields.Float(
        string='Best Seller',
        help='Rata-rata penjualan bulanan dari N bulan terakhir '
             '(N = Best Seller Months pada settings).',
        compute='_compute_analysis_preview',
        store=True,
        readonly=True,
    )

    _sql_constraints = [
        ('product_location_company_uniq',
         'unique(product_id, location_id, company_id)',
         'An alert rule already exists for this product and location!'),
    ]

    # ------------------------------------------------------------------ #
    # Stock status
    # ------------------------------------------------------------------ #

    @api.depends('product_id', 'location_id', 'min_qty')
    def _compute_qty(self):
        """Compute current on-hand and state for many rules in ONE grouped query.

        Dashboard shows REAL physical quantity per (product, location) only.
        Bahan baku equivalence (``raw_material_ids``) is intentionally NOT
        added here — it lives only in Preview Analysis + Saved Reports
        (``fky.stock.analysis.service.analyze`` with ``combine='min'``).

        The old implementation ran one ``stock.quant`` search per rule
        (N+1 queries) and re-fetched the config record for every row; this
        version aggregates all rules into a single read_group and reads the
        config once.
        """
        config = self.env['fky.stock.config'].sudo()._get()
        low_pct = config.low_stock_percent / 100.0
        urgent_pct = config.urgent_stock_percent / 100.0

        grouped = {}
        valid = self.filtered(lambda r: r.product_id and r.location_id)
        if valid:
            Quant = self.env['stock.quant'].sudo()
            domain = [
                ('product_id', 'in', valid.mapped('product_id').ids),
                ('location_id', 'in', valid.mapped('location_id').ids),
            ]
            companies = valid.mapped('company_id')
            if companies:
                domain.append(('company_id', 'in', companies.ids))
            groups = Quant.read_group(
                domain,
                ['product_id', 'location_id', 'quantity:sum'],
                ['product_id', 'location_id'], lazy=False,
            )
            for group in groups:
                if group.get('product_id') and group.get('location_id'):
                    grouped[(group['product_id'][0], group['location_id'][0])] = group['quantity'] or 0.0

        for rec in self:
            qty = grouped.get((rec.product_id.id, rec.location_id.id), 0.0) \
                if rec.product_id and rec.location_id else 0.0
            rec.qty_available = qty
            min_qty = rec.min_qty or 0.0
            if min_qty <= 0.0 or qty >= min_qty * low_pct:
                rec.state = 'ok'
            elif qty >= min_qty * urgent_pct:
                rec.state = 'low'
            else:
                rec.state = 'urgent'

    # ------------------------------------------------------------------ #
    # Counterpart-location stock (toko <-> gudang)
    # ------------------------------------------------------------------ #

    @api.depends('product_id', 'location_id')
    def _compute_other_stock(self):
        """Show the same product's on-hand at the branch's other location.

        A rule on a toko (TK...) location displays the gudang (GD...) stock
        of the same branch, and vice versa, so the dashboard answers
        "can I move stock across?" without opening the other rule. Plain
        on-hand only — no bahan baku equivalence here.
        """
        for rec in self:
            rec.other_location_id = False
            rec.other_qty_available = 0.0
        valid = self.filtered(lambda r: r.product_id and r.location_id)
        if not valid:
            return
        Location = self.env['stock.location'].sudo()
        by_name = {
            loc.complete_name: loc
            for loc in Location.search([
                ('complete_name', 'in', list(self._COUNTERPART_MAP)),
            ])
        }
        pairs = []
        for rec in valid:
            other = by_name.get(
                self._COUNTERPART_MAP.get(rec.location_id.complete_name))
            rec.other_location_id = other or False
            if other:
                pairs.append((rec, other))
        if not pairs:
            return
        grouped = {}
        groups = self.env['stock.quant'].sudo().read_group(
            [('product_id', 'in', valid.mapped('product_id').ids),
             ('location_id', 'in', list({loc.id for _, loc in pairs}))],
            ['product_id', 'location_id', 'quantity:sum'],
            ['product_id', 'location_id'], lazy=False,
        )
        for group in groups:
            if group.get('product_id') and group.get('location_id'):
                grouped[(group['product_id'][0], group['location_id'][0])] = \
                    group['quantity'] or 0.0
        for rec, other in pairs:
            rec.other_qty_available = grouped.get((rec.product_id.id, other.id), 0.0)

    # ------------------------------------------------------------------ #
    # Purchase analysis preview
    # ------------------------------------------------------------------ #

    @api.depends('product_id', 'location_id')
    def _compute_analysis_preview(self):
        config = self.env['fky.stock.config'].sudo()._get()
        best_months = config.best_seller_months or 0
        service = self.env['fky.stock.analysis.service']
        companies = self.mapped('company_id')
        no_company = self.filtered(lambda r: not r.company_id)
        for recset in [no_company] + [self.filtered(lambda r, c=c: r.company_id == c) for c in companies]:
            if not recset:
                continue
            company = recset[0].company_id
            metrics = {}
            best_metrics = {}
            valid = recset.filtered(lambda r: r.product_id and r.location_id)
            if valid and company:
                metrics = service.analyze(
                    company, valid.product_id,
                    config.average_months, config.stock_coverage_months,
                )
                if best_months:
                    best_metrics = service.average_sales(
                        company, valid.product_id, best_months)
            for rec in recset:
                rec.incoming_qty = 0.0
                rec.average_monthly_sales = 0.0
                rec.future_months_of_stock = 0.0
                rec.recommended_purchase_qty = 0.0
                rec.vendor_id = False
                rec.best_seller_average_sales = 0.0
                if not rec.product_id or not rec.location_id:
                    continue
                m = metrics.get(rec.product_id.id, {})
                rec.incoming_qty = m.get('incoming_qty', 0.0)
                rec.average_monthly_sales = m.get('average_monthly_sales', 0.0)
                rec.future_months_of_stock = m.get('future_months_of_stock', 0.0)
                rec.recommended_purchase_qty = m.get('recommended_purchase_qty', 0.0)
                rec.vendor_id = m.get('vendor_id')
                rec.best_seller_average_sales = best_metrics.get(rec.product_id.id, 0.0)

    def _compute_best_seller_values(self):
        """Recompute the stored Best Seller figure from live sales history.

        NOT a field compute: it returns ``{id: average_monthly_sales}`` for
        every record in ``self`` (callers persist it via
        :meth:`_flush_best_seller`). Only the historical-sales read runs (one
        grouped query per company), which is far cheaper than the full
        :meth:`_compute_analysis_preview`.
        """
        values = {rec.id: 0.0 for rec in self}
        config = self.env['fky.stock.config'].sudo()._get()
        best_months = config.best_seller_months or 0
        if not self or best_months <= 0:
            return values
        service = self.env['fky.stock.analysis.service']
        for company in self.mapped('company_id'):
            recset = self.filtered(lambda r, c=company: r.company_id == c)
            valid = recset.filtered(lambda r: r.product_id)
            if company and valid:
                averages = service.average_sales(company, valid.product_id, best_months)
                for rec in valid:
                    values[rec.id] = averages.get(rec.product_id.id, 0.0)
        return values

    def _flush_qty_fields(self):
        """Persist computed qty_available/state with one write per value set.

        Rules sharing the same values are updated in a single multi-record
        ``write`` (a single dict), which keeps downstream ``write`` overrides
        (e.g. ``simplify_access_management``) happy while avoiding N writes.
        """
        by_vals = {}
        for rec in self:
            by_vals.setdefault((rec.qty_available, rec.state), []).append(rec.id)
        for (qty, state), ids in by_vals.items():
            self.browse(ids).sudo().write({
                'qty_available': qty,
                'state': state,
            })

    @api.model
    def _refresh_for_pairs(self, pairs):
        """Recompute qty/state for the rules matching a set of dirty pairs.

        ``pairs`` is an iterable of ``(product_id, location_id)``. Only the
        affected rules are recomputed, and only ONE grouped quant read is
        issued for the whole set — this is what makes a bulk quant write cost
        a single recompute instead of one per row.
        """
        pairs = set(pairs)
        if not pairs:
            return
        products = {pid for pid, _lid in pairs}
        locations = {lid for _pid, lid in pairs}
        rules = self.sudo().search([
            ('product_id', 'in', list(products)),
            ('location_id', 'in', list(locations)),
        ])
        # Keep only the rules whose exact (product, location) pair is dirty.
        rules = rules.filtered(
            lambda r: (r.product_id.id, r.location_id.id) in pairs)
        if rules:
            rules._compute_qty()
            rules._flush_qty_fields()

    def _flush_best_seller(self, values):
        """Persist a {id: value} best-seller mapping with one write per value."""
        by_vals = {}
        for rec in self:
            by_vals.setdefault(values[rec.id], []).append(rec.id)
        for val, ids in by_vals.items():
            self.browse(ids).sudo().write({
                'best_seller_average_sales': val,
            })

    def search_read(self, domain=None, fields=None, offset=0, limit=None, order=None):
        """Plain read: freshness is owned by the nightly cron.

        Best Seller / qty figures are refreshed off-hours by
        :meth:`cron_nightly_refresh` (plus live quant hooks), so rendering
        the dashboard never writes — pure read, safe for any scale.
        """
        return super().search_read(domain, fields, offset, limit, order)

    @api.model
    def cron_nightly_refresh(self):
        """Off-hours refresh of all alert rules (ir.cron, ~01:00).

        Recomputes Best Seller averages plus qty/state for every rule and
        persists them. This reconciles anything the live ``stock.quant``
        hooks may have missed (their failures are swallowed by design) and
        drifts the hooks never see (e.g. BoM / raw-ratio edits, which are
        not rule dependencies).
        """
        rules = self.sudo().search([])
        if not rules:
            return True
        rules._compute_qty()
        rules._flush_qty_fields()
        rules._flush_best_seller(rules._compute_best_seller_values())
        return True

    def action_run_analysis(self):
        self.ensure_one()
        wizard = self.env['fky.stock.analysis.wizard'].create({
            'company_id': self.company_id.id,
            'product_ids': [(6, 0, self.product_id.ids)],
        })
        return {
            'name': 'Stock Purchase Analysis',
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
            'context': {'form_view_initial_mode': 'edit'},
        }

    # ------------------------------------------------------------------ #
    # Snooze
    # ------------------------------------------------------------------ #

    @api.depends('snooze_until')
    def _compute_is_snoozed(self):
        today = fields.Date.context_today(self)
        for rec in self:
            rec.is_snoozed = bool(rec.snooze_until and rec.snooze_until >= today)

    def _search_is_snoozed(self, operator, value):
        today = fields.Date.context_today(self)
        snoozed_dom = ['&', ('snooze_until', '!=', False),
                       ('snooze_until', '>=', today)]
        not_snoozed_dom = ['|', ('snooze_until', '=', False),
                           ('snooze_until', '<', today)]
        if operator == '=':
            return snoozed_dom if value else not_snoozed_dom
        if operator == '!=':
            return not_snoozed_dom if value else snoozed_dom
        # exotic operators: resolve record ids the slow way
        snoozed = self.search(snoozed_dom)
        not_snoozed = self.search(not_snoozed_dom)
        if operator in ('=like', '=ilike', 'like', 'ilike'):
            target = snoozed if value else not_snoozed
        elif operator in ('!=', 'not like', 'not ilike'):
            target = not_snoozed if value else snoozed
        else:
            raise NotImplementedError(
                'Operator %s not supported on is_snoozed' % operator)
        return [('id', 'in', target.ids)]

    # ------------------------------------------------------------------ #
    # BOM / components
    # ------------------------------------------------------------------ #

    def _get_bom(self):
        """Return the first active normal BOM for this rule's product (or empty).

        Safe when the mrp module is not installed: returns an empty recordset.
        """
        self.ensure_one()
        Bom = self.env.get('mrp.bom')
        if Bom is None:
            return Bom
        prod_id = self.product_id._origin.id if hasattr(self.product_id, '_origin') else self.product_id.id
        tmpl_id = self.product_id.product_tmpl_id.id if self.product_id.product_tmpl_id else False
        if not isinstance(prod_id, int) or not tmpl_id:
            return Bom
        return Bom.search([
            ('product_tmpl_id', '=', tmpl_id),
            ('type', '=', 'normal'),
            ('active', '=', True),
        ], order='sequence, id', limit=1)

    @api.depends('product_id')
    def _compute_has_components(self):
        # one BOM search for all rules instead of one search per rule;
        # keeps the old "first active normal BOM per template" semantics
        Bom = self.env.get('mrp.bom')
        first_bom_by_tmpl = {}
        if Bom is not None:
            tmpl_ids = list({
                rec.product_id.product_tmpl_id.id
                for rec in self if rec.product_id.product_tmpl_id
            })
            if tmpl_ids:
                for bom in Bom.search([
                    ('product_tmpl_id', 'in', tmpl_ids),
                    ('type', '=', 'normal'),
                    ('active', '=', True),
                ], order='sequence, id'):
                    first_bom_by_tmpl.setdefault(bom.product_tmpl_id.id, bom)
        for rec in self:
            bom = first_bom_by_tmpl.get(
                rec.product_id.product_tmpl_id.id) if rec.product_id else None
            if bom and bom.bom_line_ids:
                rec.has_components = True
                rec.component_count = len(bom.bom_line_ids)
            else:
                rec.has_components = False
                rec.component_count = 0

    def _search_has_components(self, operator, value):
        """Search rules by whether their product has an active normal BOM.

        Resolved with a domain (``product_id in <templates with a BOM>``)
        instead of loading every rule into memory and filtering in Python —
        the old form was O(all rules) per search regardless of the page size.
        """
        Bom = self.env.get('mrp.bom')
        if Bom is None:
            tmpl_ids = []
        else:
            tmpl_ids = Bom.search([
                ('type', '=', 'normal'),
                ('active', '=', True),
            ]).mapped('product_tmpl_id').ids
        has = [('product_id.product_tmpl_id', 'in', tmpl_ids)]
        has_not = [('product_id.product_tmpl_id', 'not in', tmpl_ids)]
        if operator in ('=', '=like', '=ilike', 'like', 'ilike'):
            return has if value else has_not
        if operator in ('!=', 'not like', 'not ilike'):
            return has_not if value else has
        raise NotImplementedError(
            'Operator %s not supported on has_components' % operator)

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def action_snooze(self):
        self.ensure_one()
        wizard = self.env['fky.stock.snooze.wizard'].create({
            'rule_id': self.id,
        })
        return {
            'name': 'Snooze Product',
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.snooze.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
            'context': {'form_view_initial_mode': 'edit'},
        }

    def action_unsnooze(self):
        self.write({'snooze_until': False})
        return True

    def action_view_components(self):
        self.ensure_one()
        bom = self._get_bom()
        wizard = self.env['fky.stock.bom.wizard'].create({
            'product_id': self.product_id.id,
            'company_id': self.company_id.id,
            'bom_id': bom.display_name if bom else '',
            'line_ids': self.env['fky.stock.bom.wizard']._component_line_vals(
                bom, self.company_id),
        })
        return {
            'name': 'Product Components',
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.bom.wizard',
            'view_mode': 'form',
            'views': [(self.env.ref(
                'fky_stock_management.view_fky_stock_bom_wizard_simple_form'
            ).id, 'form')],
            'res_id': wizard.id,
            'target': 'new',
            'context': {'form_view_initial_mode': 'edit'},
        }

    def action_mass_operations(self):
        """Open the combined Mutasi / Manufacture wizard for the selected rules."""
        wizard = self.env['fky.stock.operation.wizard'].create({
            'rule_ids': [(6, 0, self.ids)],
        })
        return {
            'name': 'Mutasi / Manufacture (Draft)',
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.operation.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
            'context': {'form_view_initial_mode': 'edit'},
        }

    # ------------------------------------------------------------------ #
    # Overrides
    # ------------------------------------------------------------------ #

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._compute_qty()
        records._flush_qty_fields()
        return records

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in ['product_id', 'location_id', 'min_qty']):
            self._compute_qty()
            self._flush_qty_fields()
        return res