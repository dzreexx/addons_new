# -*- coding: utf-8 -*-
{
    "name": "FKY Sales Return Condition Fix",
    "summary": "One-off: merge duplicate condition and restore NULL conditions from an old database",
    "description": """
One-off maintenance wizard for Sales Return.

- Merges duplicate condition records (same normalized name) into a single
  canonical record.
- Reads an old (restored) database and restores
  ``sales_return_line.condition_id`` for lines whose condition is currently
  empty.

Only fills empty conditions; it never overwrites an existing value.
""",
    "author": "FKY",
    "category": "Sales",
    "version": "15.0.1.0.1",
    "depends": ["aos_sales_return"],
    "data": [
        "security/ir.model.access.csv",
        "wizard/restore_wizard_view.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
    "license": "LGPL-3",
    "uninstall_hook": "uninstall_hook",
}
