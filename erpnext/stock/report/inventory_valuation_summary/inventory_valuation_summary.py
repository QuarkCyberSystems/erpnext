# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe, erpnext
from frappe import _
from frappe.utils import flt, cint, getdate, now, date_diff
from erpnext.stock.utils import add_additional_uom_columns
from erpnext.stock.report.stock_ledger.stock_ledger import get_item_group_condition

from erpnext.stock.report.stock_ageing.stock_ageing import get_fifo_queue, get_average_age

from six import iteritems

def execute(filters=None):
	if not filters: filters = {}

	validate_filters(filters)

	from_date = filters.get('from_date')
	to_date = filters.get('to_date')

	if filters.get("company"):
		company_currency = erpnext.get_company_currency(filters.get("company"))
	else:
		company_currency = frappe.db.get_single_value("Global Defaults", "default_currency")

	include_uom = filters.get("include_uom")
	columns = get_columns(filters)
	items = get_items(filters)
	sle = get_stock_ledger_entries(filters, items)
	# print(sle)
	if filters.get('show_stock_ageing_data'):
		filters['show_warehouse_wise_stock'] = True
		item_wise_fifo_queue = get_fifo_queue(filters, sle)

	# if no stock ledger entry found return
	if not sle:
		return columns, []

	iwb_map = get_item_warehouse_map(filters, sle)
	item_map = get_item_details(items, sle, filters)
	item_reorder_detail_map = get_item_reorder_details(item_map.keys())

	data = []
	conversion_factors = {}

	_func = lambda x: x[1]

	for (company, item) in sorted(iwb_map):
		if item_map.get(item):
			qty_dict = iwb_map[(company, item)]
			item_reorder_level = 0
			item_reorder_qty = 0
			if item in item_reorder_detail_map:
				item_reorder_level = item_reorder_detail_map[item]["warehouse_reorder_level"]
				item_reorder_qty = item_reorder_detail_map[item]["warehouse_reorder_qty"]

			report_data = {
				'currency': company_currency,
				'item_code': item,
				'company': company,
				'reorder_level': item_reorder_level,
				'reorder_qty': item_reorder_qty,
			}
			report_data.update(item_map[item])
			report_data.update(qty_dict)

			if include_uom:
				conversion_factors.setdefault(item, item_map[item].conversion_factor)

			if filters.get('show_stock_ageing_data'):
				fifo_queue = item_wise_fifo_queue[(item)].get('fifo_queue')

				stock_ageing_data = {
					'average_age': 0,
					'earliest_age': 0,
					'latest_age': 0
				}
				if fifo_queue:
					fifo_queue = sorted(filter(_func, fifo_queue), key=_func)
					if not fifo_queue: continue

					stock_ageing_data['average_age'] = get_average_age(fifo_queue, to_date)
					stock_ageing_data['earliest_age'] = date_diff(to_date, fifo_queue[0][1])
					stock_ageing_data['latest_age'] = date_diff(to_date, fifo_queue[-1][1])

				report_data.update(stock_ageing_data)

			data.append(report_data)

	add_additional_uom_columns(columns, data, include_uom, conversion_factors)
	item_group_map = get_item_group_map(data)

	top_level_item_groups = [d for d in item_group_map.values() if not d.parent_item_group]

	tree_data = []
	for d in top_level_item_groups:
		build_tree_data(tree_data, d, company_currency, 0)

	accumulate_item_values_in_parent(data, item_group_map, columns)
	tree_data = remove_empty_groups(tree_data, item_group_map)
	return columns, tree_data

def get_item_group_map(item_rows):
	# dictionary that maps item group to it's parent item group
	item_group_list = frappe.get_all("Item Group", fields=['name', 'parent_item_group'])
	item_group_map = {}

	for d in item_group_list:
		d.children_names = []
		d.children = []
		d.items = []
		item_group_map[d.name] = d

	# create a flat dictionary first... heirarchy later
	for d in item_group_map.values():
		if item_group_map.get(d.parent_item_group):
			item_group_map[d.parent_item_group].children_names.append(d.name)

	# now create the hierarchy
	for d in item_group_map.values():
		for child_name in d.children_names:
			d.children.append(item_group_map[child_name])

	for row in item_rows:
		item_group_map[row['item_group']]['items'].append(row)

	return item_group_map

def build_tree_data(tree_data, d, company_currency, indent=0):
	item_group_heading = {"item_group": d.name, "indent": indent, "_bold": 1, "currency": company_currency}
	item_group_total = item_group_heading.copy()

	item_group_heading.update({"item_code": "'Group: {0}'".format(d.name), "is_group_heading": 1})
	item_group_total.update({"item_code": "'Total: {0}'".format(d.name), "is_group_total": 1})

	d['report_row'] = item_group_total
	tree_data.append(item_group_heading)
	for i in d['items']:
		i['indent'] = indent + 1
		tree_data.append(i)
	for ch in d.children:
		build_tree_data(tree_data, ch, company_currency, indent + 1)

	tree_data.append(item_group_total)

def remove_empty_groups(tree_data, item_group_map):
	item_groups = [key for key,val in item_group_map.items() if val['items']]

	def intersection(lst1, lst2):
		return list(set(lst1) & set(lst2))

	for key, val in item_group_map.items():
		if intersection(val['children_names'],item_groups):
			item_groups.append(key)

	new_tree_data = [i for i in tree_data if i['item_group'] in item_groups]

	return new_tree_data

def accumulate_item_values_in_parent(item_rows, item_group_map, columns):
	sum_fieldnames = [d['fieldname'] for d in columns if d.get('convertible') in ('qty', 'currency')]
	avg_fieldnames = ['val_rate']

	def accumulate(source, target):
		for fn in sum_fieldnames:
			target[fn] += source[fn]

	for item_group_row in item_group_map.values():
		for fn in sum_fieldnames:
			item_group_row.report_row[fn] = 0

	for row in item_rows:
		item_group_row = item_group_map[row['item_group']]
		accumulate(row, item_group_row.report_row)

	leaf_item_groups = [d for d in item_group_map.values() if not d['children']]
	for leaf_item_group_dict in leaf_item_groups:
		current_item_group_dict = leaf_item_group_dict
		parent_item_group_dict = item_group_map.get(current_item_group_dict.parent_item_group)
		while parent_item_group_dict:
			accumulate(current_item_group_dict.report_row, parent_item_group_dict.report_row)
			current_item_group_dict = parent_item_group_dict
			parent_item_group_dict = item_group_map.get(current_item_group_dict.parent_item_group)


def get_columns(filters):
	"""return columns"""
	columns = [
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 250},
		{"label": _("Item Group"), "fieldname": "item_group", "fieldtype": "Link", "options": "Item Group", "width": 100},
		{"label": _("Stock UOM"), "fieldname": "stock_uom", "fieldtype": "Link", "options": "UOM", "width": 90},
		{"label": _("Balance Qty"), "fieldname": "bal_qty", "fieldtype": "Float", "width": 100, "convertible": "qty"},
		{"label": _("Balance Value"), "fieldname": "bal_val", "fieldtype": "Currency", "width": 100, "options": "currency", "convertible": "currency"},
		{"label": _("Opening Qty"), "fieldname": "opening_qty", "fieldtype": "Float", "width": 100, "convertible": "qty"},
		{"label": _("Opening Value"), "fieldname": "opening_val", "fieldtype": "Currency", "width": 110, "options": "currency", "convertible": "currency"},
		{"label": _("In Qty"), "fieldname": "in_qty", "fieldtype": "Float", "width": 80, "convertible": "qty"},
		{"label": _("In Value"), "fieldname": "in_val", "fieldtype": "Currency", "options": "currency", "width": 80, "convertible": "currency"},
		{"label": _("Out Qty"), "fieldname": "out_qty", "fieldtype": "Float", "width": 80, "convertible": "qty"},
		{"label": _("Out Value"), "fieldname": "out_val", "fieldtype": "Currency", "options": "currency", "width": 80, "convertible": "currency"},
		{"label": _("Valuation Rate"), "fieldname": "val_rate", "fieldtype": "Currency", "width": 90, "convertible": "rate", "options": "currency"},
		{"label": _("Reorder Level"), "fieldname": "reorder_level", "fieldtype": "Float", "width": 80, "convertible": "qty"},
		{"label": _("Reorder Qty"), "fieldname": "reorder_qty", "fieldtype": "Float", "width": 80, "convertible": "qty"},
		{"label": _("Company"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 100}
	]

	if filters.get('show_stock_ageing_data'):
		columns += [{'label': _('Average Age'), 'fieldname': 'average_age', 'width': 100},
		{'label': _('Earliest Age'), 'fieldname': 'earliest_age', 'width': 100},
		{'label': _('Latest Age'), 'fieldname': 'latest_age', 'width': 100}]

	if filters.get('show_variant_attributes'):
		columns += [{'label': att_name, 'fieldname': att_name, 'width': 100} for att_name in get_variants_attributes()]

	return columns

def get_conditions(filters):
	conditions = ""
	if not filters.get("from_date"):
		frappe.throw(_("'From Date' is required"))

	if filters.get("to_date"):
		conditions += " and sle.posting_date <= %s" % frappe.db.escape(filters.get("to_date"))
	else:
		frappe.throw(_("'To Date' is required"))

	if filters.get("company"):
		conditions += " and sle.company = %s" % frappe.db.escape(filters.get("company"))

	if filters.get("warehouse"):
		warehouse_details = frappe.db.get_value("Warehouse",
			filters.get("warehouse"), ["lft", "rgt"], as_dict=1)
		if warehouse_details:
			conditions += " and exists (select name from `tabWarehouse` wh \
				where wh.lft >= %s and wh.rgt <= %s and sle.warehouse = wh.name)"%(warehouse_details.lft,
				warehouse_details.rgt)

	if filters.get("warehouse_type") and not filters.get("warehouse"):
		conditions += " and exists (select name from `tabWarehouse` wh \
			where wh.warehouse_type = '%s' and sle.warehouse = wh.name)"%(filters.get("warehouse_type"))

	return conditions

def get_stock_ledger_entries(filters, items):
	item_conditions_sql = ''
	if items:
		item_conditions_sql = ' and sle.item_code in ({})'\
			.format(', '.join([frappe.db.escape(i, percent=False) for i in items]))

	conditions = get_conditions(filters)

	return frappe.db.sql("""
		select
			sle.item_code, sle.posting_date, sle.actual_qty, sle.valuation_rate,
			sle.company, sle.voucher_type, sle.qty_after_transaction, sle.stock_value_difference,
			sle.item_code as name, sle.voucher_no
		from
			`tabStock Ledger Entry` sle force index (posting_sort_index)
		where sle.docstatus < 2 %s %s
		order by sle.posting_date, sle.posting_time, sle.creation, sle.actual_qty""" % #nosec
		(item_conditions_sql, conditions), as_dict=1)

def get_item_warehouse_map(filters, sle):
	iwb_map = {}
	from_date = getdate(filters.get("from_date"))
	to_date = getdate(filters.get("to_date"))

	float_precision = cint(frappe.db.get_default("float_precision")) or 3

	for d in sle:
		key = (d.company, d.item_code)
		if key not in iwb_map:
			iwb_map[key] = frappe._dict({
				"opening_qty": 0.0, "opening_val": 0.0,
				"in_qty": 0.0, "in_val": 0.0,
				"out_qty": 0.0, "out_val": 0.0,
				"bal_qty": 0.0, "bal_val": 0.0,
				"val_rate": 0.0
			})

		qty_dict = iwb_map[(d.company, d.item_code)]

		if d.voucher_type == "Stock Reconciliation":
			qty_diff = flt(d.qty_after_transaction) - flt(qty_dict.bal_qty)
		else:
			qty_diff = flt(d.actual_qty)

		value_diff = flt(d.stock_value_difference)

		if d.posting_date < from_date:
			qty_dict.opening_qty += qty_diff
			qty_dict.opening_val += value_diff

		elif d.posting_date >= from_date and d.posting_date <= to_date:
			if flt(qty_diff, float_precision) >= 0:
				qty_dict.in_qty += qty_diff
				qty_dict.in_val += value_diff
			else:
				qty_dict.out_qty += abs(qty_diff)
				qty_dict.out_val += abs(value_diff)

		qty_dict.val_rate = d.valuation_rate
		qty_dict.bal_qty += qty_diff
		qty_dict.bal_val += value_diff

	iwb_map = filter_items_with_no_transactions(iwb_map, float_precision)

	return iwb_map

def filter_items_with_no_transactions(iwb_map, float_precision):
	for (company, item) in sorted(iwb_map):
		qty_dict = iwb_map[(company, item)]

		no_transactions = True
		for key, val in iteritems(qty_dict):
			val = flt(val, float_precision)
			qty_dict[key] = val
			if key != "val_rate" and val:
				no_transactions = False

		if no_transactions:
			iwb_map.pop((company, item))

	return iwb_map

def get_items(filters):
	conditions = []
	if filters.get("item_code"):
		conditions.append("item.name=%(item_code)s")
	else:
		if filters.get("item_group"):
			conditions.append(get_item_group_condition(filters.get("item_group")))

	items = []
	if conditions:
		items = frappe.db.sql_list("""select name from `tabItem` item where {}"""
			.format(" and ".join(conditions)), filters)
	return items

def get_item_details(items, sle, filters):
	item_details = {}
	if not items:
		items = list(set([d.item_code for d in sle]))

	if not items:
		return item_details

	cf_field = cf_join = ""
	if filters.get("include_uom"):
		cf_field = ", ucd.conversion_factor"
		cf_join = "left join `tabUOM Conversion Detail` ucd on ucd.parent=item.name and ucd.uom=%s" \
			% frappe.db.escape(filters.get("include_uom"))

	res = frappe.db.sql("""
		select
			item.name, item.item_name, item.description, item.item_group, item.brand, item.stock_uom %s
		from
			`tabItem` item
			%s
		where
			item.name in (%s)
	""" % (cf_field, cf_join, ','.join(['%s'] *len(items))), items, as_dict=1)

	for item in res:
		item_details.setdefault(item.name, item)

	if filters.get('show_variant_attributes', 0) == 1:
		variant_values = get_variant_values_for(list(item_details))
		item_details = {k: v.update(variant_values.get(k, {})) for k, v in iteritems(item_details)}

	return item_details

def get_item_reorder_details(items):
	item_reorder_details = frappe._dict()

	if items:
		item_reorder_details = frappe.db.sql("""
			select parent, warehouse, warehouse_reorder_qty, warehouse_reorder_level
			from `tabItem Reorder`
			where parent in ({0})
		""".format(', '.join([frappe.db.escape(i, percent=False) for i in items])), as_dict=1)

	return dict((d.parent + d.warehouse, d) for d in item_reorder_details)

def validate_filters(filters):
	if not (filters.get("item_code") or filters.get("warehouse")):
		sle_count = flt(frappe.db.sql("""select count(name) from `tabStock Ledger Entry`""")[0][0])
		if sle_count > 500000:
			frappe.throw(_("Please set filter based on Item or Warehouse due to a large amount of entries."))

def get_variants_attributes():
	'''Return all item variant attributes.'''
	return [i.name for i in frappe.get_all('Item Attribute')]

def get_variant_values_for(items):
	'''Returns variant values for items.'''
	attribute_map = {}
	for attr in frappe.db.sql('''select parent, attribute, attribute_value
		from `tabItem Variant Attribute` where parent in (%s)
		''' % ", ".join(["%s"] * len(items)), tuple(items), as_dict=1):
			attribute_map.setdefault(attr['parent'], {})
			attribute_map[attr['parent']].update({attr['attribute']: attr['attribute_value']})

	return attribute_map
