# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class StockAnalysisWizardCoverageLine(models.TransientModel):
    _name = 'fky.stock.analysis.wizard.coverage.line'
    _description = 'Stock Analysis Wizard Coverage Line'

    wizard_id = fields.Many2one(
        'fky.stock.analysis.wizard', string='Wizard', ondelete='cascade')
    company_id = fields.Many2one(
        'res.company', related='wizard_id.company_id', store=False)
    internal_category_id = fields.Many2one(
        'internal.category', string='Internal Category')
    product_id = fields.Many2one(
        'product.product', string='Product',
        domain=[('type', '=', 'product'), ('active', '=', True)],
        help='Produk spesifik yang diberi ketahanan bulan khusus. '
             'Mengalahkan nilai kategori dan default.')
    coverage_months = fields.Integer(
        string='Ketahanan Bulan',
        help='Target ketahanan stok khusus (dalam bulan). Isi tepat satu: '
             'Internal Category ATAU Product. Baris kosong akan diabaikan '
             'saat generate.')

    @api.constrains('product_id', 'internal_category_id')
    def _check_single_target(self):
        for line in self:
            if line.product_id and line.internal_category_id:
                raise ValidationError(
                    _('Pilih salah satu per baris: Internal Category ATAU '
                      'Product, tidak boleh keduanya.'))


class StockAnalysisWizard(models.TransientModel):
    """One-shot generation of a purchase-analysis preview.

    The heavy lifting is delegated to ``fky.stock.analysis.service`` so the
    numbers always match the real-time preview on the stock-rule dashboard.
    Rows are shown in an editable :model:`fky.stock.analysis.preview` where
    the user ticks the products to purchase; nothing is persisted until
    ``Save Report`` is pressed, which materialises a
    :model:`fky.stock.analysis.report`.
    """

    _name = 'fky.stock.analysis.wizard'
    _description = 'Generate Stock Purchase Analysis Report'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
        help='Perusahaan yang dipakai untuk data stok dan penjualan.')

    average_months = fields.Integer(
        string='Average Month', required=True,
        default=lambda self: self._default_average_months(),
        help='Jumlah bulan riwayat penjualan untuk menghitung rata-rata '
             'penjualan bulanan (4 atau 12 bulan).')

    stock_coverage_months = fields.Integer(
        string='Ketahanan Bulan (Default)', required=True,
        default=lambda self: self._default_coverage_months(),
        help='Target ketahanan stok default (dalam bulan) untuk produk yang '
              'tidak punya nilai khusus. Nilai khusus per internal category '
              'atau per product bisa diisi di grid bawah; product '
              'mengalahkan kategori.')

    coverage_line_ids = fields.One2many(
        'fky.stock.analysis.wizard.coverage.line', 'wizard_id',
        string='Ketahanan Bulan per Internal Category',
        domain=[('internal_category_id', '!=', False)],
        help='Isi ketahanan bulan khusus per internal category (mis. CDI, '
              'carburator). Semua produk dalam kategori yang tidak '
              'dicantumkan memakai nilai default di atas, kecuali produk '
              'tersebut punya nilai khusus di grid per product.')

    product_coverage_line_ids = fields.One2many(
        'fky.stock.analysis.wizard.coverage.line', 'wizard_id',
        string='Ketahanan Bulan per Product',
        domain=[('product_id', '!=', False)],
        help='Isi ketahanan bulan khusus per product. Nilai di sini '
              'mengalahkan nilai kategori dan default di atas.')

    product_ids = fields.Many2many(
        'product.product', string='Filter Product Name',
        help='Opsional. Kosongkan untuk menganalisa SEMUA produk storable '
             'di semua warehouse.')

    internal_category_ids = fields.Many2many(
        'internal.category', string='Internal Category',
        help='Opsional. Filter berdasarkan kategori internal produk '
             '(mis. carburator, CDI, as gear depan).')

    target_report_id = fields.Many2one(
        'fky.stock.analysis.report', string='Previous Report',
        help='Bila diisi, hasil generate disimpan sebagai versi baru dari '
             'report ini (dipakai tombol Regenerate).')

    coverage_loaded = fields.Boolean(
        string='Coverage Loaded',
        default=False,
        help='Technical: True bila grid ketahanan sudah dimuat penuh dari '
             'saved rules. Cleanup baris yang dihapus hanya jalan bila '
             'True, agar wizard yang dibuat parsial tidak menghapus rules.')

    # ------------------------------------------------------------------ #
    # Defaults (delegated to the singleton config object)
    # ------------------------------------------------------------------ #

    @api.model
    def _default_average_months(self):
        return self._company_average_months(self.company_id or self.env.company)

    @api.model
    def _company_average_months(self, company):
        """PT Adireksa averages 12 months; PT Annapurna averages 4 months."""
        if company and company.name and 'annapurna' in company.name.lower():
            return 4
        return 12

    @api.onchange('company_id')
    def _onchange_company_id(self):
        self.average_months = self._company_average_months(self.company_id)
        self._load_coverage_lines()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            if rec.coverage_loaded:
                # Grids were already resolved client-side (prefilled via
                # onchange, user may even have emptied them): never reload,
                # or rows deleted before the first save would resurrect.
                continue
            if not rec.coverage_line_ids \
                    and not rec.product_coverage_line_ids:
                rec._load_coverage_lines()
            else:
                rec.coverage_loaded = False
        return records

    def _load_coverage_lines(self):
        """Prefill per-category and per-product coverage from saved rules."""
        self.ensure_one()
        Rule = self.env['fky.stock.coverage.rule']
        rules = Rule.search([
            '|', ('company_id', '=', False),
            ('company_id', '=', self.company_id.id),
            ('active', '=', True),
        ])
        categ_vals = [(0, 0, {
            'internal_category_id': rule.internal_category_id.id,
            'coverage_months': rule.coverage_months,
        }) for rule in rules
            if rule.internal_category_id
            and 'internal.category' in self.env]
        product_vals = [(0, 0, {
            'product_id': rule.product_id.id,
            'coverage_months': rule.coverage_months,
        }) for rule in rules if rule.product_id]
        self.coverage_line_ids = categ_vals
        self.product_coverage_line_ids = product_vals
        self.coverage_loaded = True
        return self.coverage_line_ids

    def _save_coverage_lines(self):
        """Persist wizard coverage grids into saved rules.

        Remembered so the next report automatically uses the same values.
        A row with a zero / empty coverage removes the saved rule — and so
        does DELETING the row in the wizard: any previously loaded rule
        whose target no longer appears in the grids is unlinked, otherwise
        deleted rows would resurrect on the next generate (the old code
        only visited rows still present). Rows with neither target are
        ignored; rows with both targets raise. Batched: one search per
        scope instead of one per row.
        """
        self.ensure_one()
        Rule = self.env['fky.stock.coverage.rule']
        has_categ_model = 'internal.category' in self.env
        categ_ids = [
            line.internal_category_id.id
            for line in self.coverage_line_ids if line.internal_category_id
        ] if has_categ_model else []
        product_ids = [
            line.product_id.id
            for line in self.product_coverage_line_ids if line.product_id
        ]
        existing_categ = {}
        existing_product = {}
        # NOTE: include inactive rules (active_test=False): an archived rule
        # still occupies the unique (target, company) slot, so saving must
        # reactivate it via write instead of crashing on create.
        RuleAll = Rule.with_context(active_test=False)
        if categ_ids:
            for rule in RuleAll.search([
                ('internal_category_id', 'in', categ_ids),
                '|', ('company_id', '=', False),
                ('company_id', '=', self.company_id.id),
            ]):
                existing_categ.setdefault(
                    rule.internal_category_id.id, rule)
        if product_ids:
            for rule in RuleAll.search([
                ('product_id', 'in', product_ids),
                '|', ('company_id', '=', False),
                ('company_id', '=', self.company_id.id),
            ]):
                existing_product.setdefault(rule.product_id.id, rule)
        if has_categ_model:
            for line in self.coverage_line_ids:
                self._persist_coverage_line(
                    line.internal_category_id and
                    {'internal_category_id': line.internal_category_id.id},
                    line.coverage_months,
                    existing_categ.get(line.internal_category_id.id)
                    if line.internal_category_id else None)
        for line in self.product_coverage_line_ids:
            self._persist_coverage_line(
                line.product_id and {'product_id': line.product_id.id},
                line.coverage_months,
                existing_product.get(line.product_id.id)
                if line.product_id else None)
        # Cleanup deleted rows (only when grids were fully loaded, see
        # coverage_loaded): any applicable rule that was loaded into
        # the grids (same domain as _load_coverage_lines) but whose target
        # is gone from the grids was deleted by the user -> remove it so
        # it does not resurrect on the next generate. Only active rules
        # are considered, mirroring _load; archived rules are left alone.
        # Scoped to company False/mine so other companies' rules are safe.
        if not self.coverage_loaded:
            return
        if has_categ_model:
            stale_categ = Rule.search([
                ('internal_category_id', '!=', False),
                ('active', '=', True),
                '|', ('company_id', '=', False),
                ('company_id', '=', self.company_id.id),
            ]).filtered(
                lambda r: r.internal_category_id.id not in categ_ids)
            if stale_categ:
                stale_categ.unlink()
        stale_product = Rule.search([
            ('product_id', '!=', False),
            ('active', '=', True),
            '|', ('company_id', '=', False),
            ('company_id', '=', self.company_id.id),
        ]).filtered(lambda r: r.product_id.id not in product_ids)
        if stale_product:
            stale_product.unlink()

    def _persist_coverage_line(self, target_vals, coverage_months, rule):
        """Create / update / unlink one saved coverage rule."""
        self.ensure_one()
        if not target_vals:
            return
        if coverage_months and coverage_months > 0:
            if rule:
                rule.write({
                    'coverage_months': coverage_months,
                    'active': True,
                    'company_id': self.company_id.id,
                })
            else:
                self.env['fky.stock.coverage.rule'].create(dict(
                    target_vals,
                    coverage_months=coverage_months,
                    company_id=self.company_id.id,
                ))
        elif rule:
            rule.unlink()

    @api.model
    def _default_coverage_months(self):
        config = self.env['fky.stock.config'].sudo()._get()
        return config.stock_coverage_months or 3

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    @api.constrains('average_months', 'stock_coverage_months')
    def _check_analysis_months(self):
        for wizard in self:
            if wizard.average_months < 1:
                raise ValidationError(_('Average sales months must be at least 1.'))
            if wizard.stock_coverage_months <= 0:
                raise ValidationError(_('Stock coverage must be a positive number of months.'))

    def _validate_generation_parameters(self):
        for wizard in self:
            if not wizard.company_id:
                raise ValidationError(_('A company is required to run the analysis.'))

    def _internal_category_command(self):
        """Build an m2m command, tolerating a missing internal.category model.

        The internal.category comodel comes from an optional addon; when it is
        not loaded the field comodel is left as ``_unknown`` by the registry,
        so reading ``self.internal_category_ids`` would fail.  Return an empty
        command instead.
        """
        if not self.env.get('internal.category'):
            return [(6, 0, [])]
        return [(6, 0, self.internal_category_ids.ids)]

    # ------------------------------------------------------------------ #
    # Candidate products
    # ------------------------------------------------------------------ #

    def _product_candidates(self):
        self.ensure_one()
        products = self.env['product.product'].search([
            ('type', '=', 'product'),
            ('active', '=', True),
        ])
        if self.product_ids:
            products = products & self.product_ids
        if self.internal_category_ids:
            if 'internal_category' in self.env['product.product']._fields:
                products = products & self.env['product.product'].search([
                    ('internal_category', 'in', self.internal_category_ids.ids),
                ])
            else:
                raise ValidationError(
                    _('Internal categories require the AOS Product module '
                      '(field internal_category is missing on products).'))
        return products.with_company(self.company_id.id)

    # ------------------------------------------------------------------ #
    # Generate
    # ------------------------------------------------------------------ #

    @api.model
    def open_wizard(self):
        """Create a persisted wizard and open it for editing.

        Used by the menu (instead of opening an empty virtual form): the
        coverage grids are loaded server-side into REAL line records, so
        row deletions are real unlinks that survive to Generate. A purely
        virtual dialog cannot reliably report pre-save deletions back to
        the server (non-view onchange state is not sent on create), which
        made deleted rows resurrect on every Generate.
        """
        wizard = self.create({
            'company_id': self.env.company.id,
        })
        return {
            'name': _('Stock Purchase Analysis'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
            'context': {'form_view_initial_mode': 'edit'},
        }

    def action_save_coverage(self):
        """Persist the coverage grids without generating.

        Explicit save point for ketahanan-bulan edits (including row
        deletions, which permanently remove the saved rule right away).
        Stays in the dialog so the user can continue to Generate.
        Generate itself still saves as well, so unsaved last edits are
        never silently dropped from a run.
        """
        self.ensure_one()
        self._save_coverage_lines()
        categ_count = len([
            line for line in self.coverage_line_ids
            if line.internal_category_id
        ])
        product_count = len([
            line for line in self.product_coverage_line_ids
            if line.product_id
        ])
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Coverage Saved'),
                'message': _(
                    '%d kategori + %d produk tersimpan. Baris yang dihapus '
                    'ikut terhapus permanen.') % (categ_count, product_count),
                'type': 'success',
                'sticky': False,
            },
        }

    def action_generate(self):
        """Build an editable preview instead of persisting a report.

        The user reviews the rows, ticks the ones to purchase and hits
        ``Save Report`` (on the preview form) to persist them as a
        :model:`fky.stock.analysis.report`.
        """
        self.ensure_one()
        self._validate_generation_parameters()
        self._save_coverage_lines()
        products = self._product_candidates()

        service = self.env['fky.stock.analysis.service']
        values = service.analyze(
            self.company_id, products, self.average_months,
            self.stock_coverage_months, combine='min')

        preview = self.env['fky.stock.analysis.preview'].create({
            'company_id': self.company_id.id,
            'average_months': self.average_months,
            'stock_coverage_months': self.stock_coverage_months,
            'product_ids': [(6, 0, self.product_ids.ids)],
            'internal_category_ids': self._internal_category_command(),
            'target_report_id': self.target_report_id.id,
        })

        saved_moq = {}
        if self.target_report_id:
            for line in self.target_report_id.line_ids:
                saved_moq[line.product_id.id] = line.moq_qty

        # batched lookups: one dict for product access, one search each for
        # coverage rules and MoQs (was: O(N²) record filtering + a search per
        # product for both coverage and MoQ)
        id2product = {p.id: p for p in products}
        coverage_map = self.env['fky.stock.coverage.rule'].resolve_coverage_batch(
            products, self.company_id, fallback=self.stock_coverage_months)
        vendor_map = {pid: m.get('vendor_id') for pid, m in values.items()}
        moq_map = service.default_moq_batch(
            products, vendor_map, self.company_id)

        lines = []
        for product_id, metrics in values.items():
            product = id2product[product_id]
            coverage = coverage_map.get(
                product_id, self.stock_coverage_months)
            future_stock = (metrics.get('on_hand_qty', 0.0)
                            + metrics.get('incoming_qty', 0.0)
                            - metrics.get('sales_order_qty', 0.0))
            target_stock = metrics.get('average_monthly_sales', 0.0) * coverage
            # Target is NOT rounded (Ketahanan Bulan must stay on the real
            # figure); only the plannable quantity is rounded to whole dus.
            recommended = service.round_to_isi_perdus(
                max(0.0, target_stock - future_stock), product.isi_perdus)
            comps = metrics.get('components', [])
            done = {key: value for key, value in metrics.items()
                    if key != 'components'}
            done.update({
                'target_stock_qty': target_stock,
                'recommended_purchase_qty': recommended,
                'has_components': bool(comps),
            })
            lines.append((0, 0, {
                'preview_id': preview.id,
                'company_id': self.company_id.id,
                'product_id': product_id,
                'coverage_months': coverage,
                # A MoQ carried over from the previous report wins; otherwise
                # the batched remembered/seller default applies.
                'moq_qty': saved_moq.get(
                    product_id, moq_map.get(product_id, 0.0)),
                'final_qty': 0.0,
                **done,
            }))
        if lines:
            preview.preview_line_ids = lines

        return {
            'name': _('Stock Purchase Analysis Preview'),
            'type': 'ir.actions.act_window',
            'res_model': 'fky.stock.analysis.preview',
            'view_mode': 'form',
            'res_id': preview.id,
            'target': 'main',
        }