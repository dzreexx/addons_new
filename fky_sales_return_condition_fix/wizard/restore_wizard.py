# -*- coding: utf-8 -*-
import logging

import psycopg2

from odoo import fields, models, sql_db
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


def _norm(value):
    return (value or "").strip().lower()


class FkyReturnConditionFix(models.TransientModel):
    _name = "fky.return.condition.fix"
    _description = "Fix Sales Return Condition"

    old_db_name = fields.Char(string="Old Database", default="zzz_old", required=True)
    old_db_host = fields.Char(string="Host (optional)")
    old_db_port = fields.Integer(string="Port", default=5432)
    old_db_user = fields.Char(string="User (optional)")
    old_db_password = fields.Char(string="Password (optional)")
    condition_name = fields.Char(string="Condition Name", default="Rusak", required=True)
    mode = fields.Selection(
        selection=[("dry", "Dry Run"), ("apply", "Apply")],
        string="Mode",
        default="dry",
        required=True,
    )
    log = fields.Text(string="Log", readonly=True)
    would_update = fields.Integer(string="Lines to update", readonly=True)
    already_filled = fields.Integer(string="Already filled", readonly=True)
    conflicts = fields.Integer(string="Conflicts", readonly=True)
    missing = fields.Integer(string="Missing in current DB", readonly=True)
    duplicates_merged = fields.Integer(string="Duplicates merged", readonly=True)
    lines_moved = fields.Integer(string="Lines moved", readonly=True)

    def _open_old_db(self):
        self.ensure_one()
        dbname = (self.old_db_name or "").strip()
        if not dbname:
            raise UserError("Old database name is required.")
        if dbname == self.env.cr.dbname:
            raise UserError("Old database must be different from the current database.")
        if self.old_db_host:
            try:
                conn = psycopg2.connect(
                    host=self.old_db_host.strip(),
                    port=self.old_db_port or 5432,
                    dbname=dbname,
                    user=(self.old_db_user or "").strip() or None,
                    password=self.old_db_password or None,
                    connect_timeout=10,
                )
            except Exception as exc:
                raise UserError("Cannot connect to %s: %s" % (dbname, exc))
            conn.set_session(readonly=True, autocommit=True)
            return conn, conn.cursor(), True
        try:
            conn = sql_db.db_connect(dbname)
            return conn, conn.cursor(), False
        except Exception as exc:
            raise UserError("Cannot connect to %s: %s" % (dbname, exc))

    def _read_old_lines(self, cr):
        cr.execute(
            """
            SELECT l.id, c.name
              FROM sales_return_line l
              JOIN res_condition_product c ON c.id = l.condition_id
             WHERE l.condition_id IS NOT NULL
            """
        )
        return cr.fetchall()

    def action_run(self):
        self.ensure_one()
        target = _norm(self.condition_name)
        if not target:
            raise UserError("Condition name is required.")

        Condition = self.env["res.condition.product"]
        Line = self.env["sales.return.line"]

        matches = Condition.search([]).filtered(
            lambda condition: _norm(condition.name) == target
        )
        if not matches:
            raise UserError(
                'Condition "%s" not found in the current database.'
                % self.condition_name
            )
        canonical = min(matches, key=lambda condition: condition.id)
        duplicates = matches - canonical

        conn, cr, remote = self._open_old_db()
        try:
            rows = self._read_old_lines(cr)
        except Exception as exc:
            raise UserError("Cannot read the old database: %s" % exc)
        finally:
            try:
                cr.close()
            finally:
                if remote:
                    conn.close()

        old_ids = [line_id for line_id, name in rows if _norm(name) == target]

        to_update = []
        already_filled = 0
        conflicts = 0
        missing = 0
        sample = []

        for start in range(0, len(old_ids), 1000):
            chunk = old_ids[start:start + 1000]
            found = Line.search_read([("id", "in", chunk)], ["id", "condition_id"])
            found_ids = {row["id"] for row in found}
            missing += len(chunk) - len(found_ids)
            for row in found:
                condition_id = row["condition_id"]
                if not condition_id:
                    to_update.append(row["id"])
                    if len(sample) < 10:
                        sample.append(row["id"])
                elif condition_id in matches.ids:
                    already_filled += 1
                else:
                    conflicts += 1

        self.would_update = len(to_update)
        self.already_filled = already_filled
        self.conflicts = conflicts
        self.missing = missing
        self.duplicates_merged = len(duplicates)
        self.lines_moved = (
            Line.search_count([("condition_id", "in", duplicates.ids)])
            if duplicates
            else 0
        )

        lines = [
            "Mode: %s" % ("DRY RUN" if self.mode == "dry" else "APPLY"),
            "Old DB: %s" % self.old_db_name,
            "Condition: %s -> id %s (canonical)" % (self.condition_name, canonical.id),
            "Duplicates found: %s" % len(duplicates),
            'Old lines with condition "%s": %s' % (self.condition_name, len(old_ids)),
            "Lines to update: %s" % len(to_update),
            "Already filled: %s" % already_filled,
            "Conflicts: %s" % conflicts,
            "Missing in current DB: %s" % missing,
            "Sample line ids: %s" % ", ".join(str(line_id) for line_id in sample),
        ]

        if self.mode == "apply":
            moved = 0
            for duplicate in duplicates:
                moved += Line.search_count([("condition_id", "=", duplicate.id)])
                Line.search([("condition_id", "=", duplicate.id)]).write(
                    {"condition_id": canonical.id}
                )
                duplicate.unlink()
            self.lines_moved = moved

            updated = 0
            for start in range(0, len(to_update), 2000):
                chunk = to_update[start:start + 2000]
                batch = Line.search(
                    [("id", "in", chunk), ("condition_id", "=", False)]
                )
                if batch:
                    batch.write({"condition_id": canonical.id})
                    updated += len(batch)
            self.would_update = updated
            lines.append(
                "Merged duplicates: %s (lines moved: %s)"
                % (len(duplicates), moved)
            )
            lines.append("UPDATED: %s" % updated)
            _logger.info(
                "fky_sales_return_condition_fix: canonical=%s updated=%s moved=%s",
                canonical.id,
                updated,
                moved,
            )

        self.log = "\n".join(lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
