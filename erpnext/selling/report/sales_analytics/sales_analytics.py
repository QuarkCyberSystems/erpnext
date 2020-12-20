# Copyright (c) 2013, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _, scrub
from frappe.utils import getdate, flt, add_to_date, add_days, cstr
from six import iteritems
from erpnext.accounts.report.financial_statements import get_cost_centers_with_children
from erpnext.accounts.utils import get_fiscal_year


month_to_number = {
	'January' : 1,
	'February' : 2,
	'March' : 3,
	'April' : 4,
	'May' : 5,
	'June' : 6,
	'July' : 7,
	'August' : 8,
	'September' : 9,
	'October' : 10,
	'November' : 11,
	'December' : 12
}

def execute(filters=None):
	return Analytics(filters).run()

class Analytics(object):
	def __init__(self, filters=None):
		self.filters = frappe._dict(filters or {})
		self.date_field = 'transaction_date' \
			if self.filters.doctype in ['Sales Order', 'Purchase Order'] else 'posting_date'
		self.months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
		self.get_period_date_ranges()
		self.entity_names = {}

	def run(self):
		self.get_data()
		self.get_columns()
		self.get_chart_data()
		return self.columns, self.data, None, self.chart

	def get_columns(self):
		self.columns = [{
			"label": _(self.filters.tree_type),
			"period_label":_(self.filters.tree_type),
			"options": self.filters.tree_type,
			"fieldname": "entity",
			"fieldtype": "Link",
			"width": 140
		}]

		show_name = False
		if self.filters.tree_type == "Customer":
			if frappe.defaults.get_global_default('cust_master_name') == "Naming Series":
				show_name = True
		if self.filters.tree_type == "Supplier":
			if frappe.defaults.get_global_default('supp_master_name') == "Naming Series":
				show_name = True
		if self.filters.tree_type == "Item":
			show_name = True

		if show_name:
			self.columns.append({
				"label": _(self.filters.tree_type + " Name"),
				"period_label": _(self.filters.tree_type + " Name"),
				"fieldname": "entity_name",
				"fieldtype": "Data",
				"width": 140
			})

		self.columns.append({
			"label": _("Total"),
			"period_label": _("Total"),
			"fieldname": "total",
			"fieldtype": "Float",
			"is_total":1,
			"width": 120
		})

		if not self.filters.start_month:
			self.filters.start_month = "January"
		if not self.filters.end_month:
			self.filters.end_month = "December"

		start_month_no = month_to_number[self.filters.start_month]
		end_month_no = month_to_number[self.filters.end_month]

		if start_month_no > end_month_no:
			frappe.throw(_("End month must be greater then start month"))

		self.month_range = list(range(start_month_no, end_month_no + 1))

		period_list = []

		for sales_person in self.sales_persons:
			if sales_person.get('has_entry') or not self.filters.with_sales_person:
				for end_date in self.periodic_daterange:
					# if end_date in self.month_range:
					period = self.get_period(end_date)
					label, key = self.get_period_label_key(period, sales_person.name)
					end_date_month = end_date.month
					if end_date_month in self.month_range:
						period_list.append(frappe._dict({
							"label": _(label),
							"period_label": period,
							"fieldname": key,
							"fieldtype": "Float",
							"period_column": True,
							"end_date_month": end_date.month,
							"end_date_year": end_date.year,
							"sales_person": cstr( sales_person.name if sales_person.name else ""),
							"width": 120
						}))

		sorted_period_list = sorted(period_list, key=lambda d: (cstr(d.sales_person), d.end_date_month, d.end_date_year))
		self.columns.extend(sorted_period_list)

	def get_period_label_key(self, period, sales_person):
		label = "{0} {1}".format(sales_person or "No Sales Person", period) if self.filters.with_sales_person else period
		key = scrub(label)
		return label, key

	def get_data(self):
		self.company_currency = frappe.get_cached_value('Company', self.filters.get("company"), "default_currency")

		if self.filters.tree_type == 'Customer':
			self.get_entries("s.customer", "s.customer_name")
			self.get_rows()

		if self.filters.tree_type == 'Supplier':
			self.get_entries("s.supplier", "s.supplier_name")
			self.get_rows()

		elif self.filters.tree_type == 'Item':
			self.get_entries("i.item_code", "i.item_name")
			self.get_rows()

		elif self.filters.tree_type == 'Brand':
			self.get_entries("i.brand")
			self.get_rows()

		elif self.filters.tree_type in ["Customer Group", "Supplier Group", "Territory", "Item Group", "Sales Person"]:
			if self.filters.tree_type == 'Customer Group':
				entity_field = "s.customer_group"
			elif self.filters.tree_type == 'Supplier Group':
				entity_field = "sup.supplier_group"
			elif self.filters.tree_type == 'Territory':
				entity_field = "s.territory"
			elif self.filters.tree_type == 'Item Group':
				entity_field = "i.item_group"
			else:
				entity_field = "sp.sales_person"
			self.get_entries(entity_field)
			self.get_groups()
			self.get_rows_by_group()

	def get_entries(self, entity_field, entity_name_field=None):
		include_sales_person = self.filters.tree_type == "Sales Person" or self.filters.sales_person or self.filters.with_sales_person
		sales_team_join = "left join `tabSales Team` sp on sp.parent = s.name and sp.parenttype = %(doctype)s" \
			if include_sales_person else ""
		additional_columns = ", sp.sales_person, ifnull(sp.allocated_percentage, 100) as allocated_percentage" \
			if include_sales_person else ""

		include_supplier = self.filters.tree_type == "Supplier Group" or self.filters.supplier_group
		supplier_table = ", `tabSupplier` sup" if include_supplier else ""
		supplier_condition = "and sup.name = s.supplier" if include_supplier else ""

		is_opening_condition = "and s.is_opening != 'Yes'" if self.filters.doctype in ['Sales Invoice', 'Purchase Invoice']\
			else ""

		entity_name_field = "{0} as entity_name, ".format(entity_name_field) if entity_name_field else ""
		if include_sales_person:
			value_field = "i.{} * ifnull(sp.allocated_percentage, 100) / 100".format(self.get_value_fieldname())
		else:
			value_field = "i.{}".format(self.get_value_fieldname())

		self.entries = frappe.db.sql("""
			select
				{entity_field} as entity,
				{entity_name_field}
				{value_field} as value_field,
				s.{date_field}
				{additional_columns}
			from 
				`tab{doctype} Item` i, `tab{doctype}` s {supplier_table} {sales_team_join}
			where i.parent = s.name and s.docstatus = 1 {supplier_condition}
				and s.company = %(company)s and s.{date_field} between %(from_date)s and %(to_date)s
				{is_opening_condition} {filter_conditions}
		""".format(
			entity_field=entity_field,
			entity_name_field=entity_name_field,
			additional_columns=additional_columns,
			value_field=value_field,
			date_field=self.date_field,
			doctype=self.filters.doctype,
			sales_team_join=sales_team_join,
			supplier_table=supplier_table,
			supplier_condition=supplier_condition,
			is_opening_condition=is_opening_condition,
			filter_conditions=self.get_conditions()
		), self.filters, as_dict=1)

		if entity_name_field:
			for d in self.entries:
				self.entity_names.setdefault(d.entity, d.entity_name)

	def get_value_fieldname(self):
		filter_to_field = {
			"Net Amount": "base_net_amount",
			"Amount": "base_amount",
			"Stock Qty": "stock_qty",
			"Contents Qty": "alt_uom_qty",
			"Transaction Qty": "qty"
		}
		return filter_to_field.get(self.filters.value_field, "base_net_amount")

	def get_value_fieldtype(self):
		filter_to_field = {
			"Net Amount": "Currency",
			"Amount": "Currency",
			"Stock Qty": "Float",
			"Contents Qty": "Float",
			"Transaction Qty": "Float"
		}
		return filter_to_field.get(self.filters.value_field, "Currency")

	def get_conditions(self):
		conditions = []

		if self.filters.get("customer"):
			conditions.append("s.customer=%(customer)s")

		if self.filters.get("customer_group"):
			lft, rgt = frappe.db.get_value("Customer Group", self.filters.customer_group, ["lft", "rgt"])
			conditions.append("""s.customer_group in (select name from `tabCustomer Group`
					where lft>=%s and rgt<=%s and docstatus<2)""" % (lft, rgt))

		if self.filters.get("supplier"):
			conditions.append("s.supplier=%(supplier)s")

		if self.filters.get("supplier_group"):
			lft, rgt = frappe.db.get_value("Supplier Group", self.filters.supplier_group, ["lft", "rgt"])
			conditions.append("""sup.supplier_group in (select name from `tabSupplier Group`
					where lft>=%s and rgt<=%s and docstatus<2)""" % (lft, rgt))

		if self.filters.get("item_code"):
			conditions.append("i.item_code=%(item_code)s")

		if self.filters.get("item_group"):
			lft, rgt = frappe.db.get_value("Item Group", self.filters.item_group, ["lft", "rgt"])
			conditions.append("""i.item_group in (select name from `tabItem Group`
					where lft>=%s and rgt<=%s and docstatus<2)""" % (lft, rgt))

		if self.filters.get("brand"):
			conditions.append("i.brand=%(brand)s")

		if self.filters.get("territory"):
			lft, rgt = frappe.db.get_value("Territory", self.filters.territory, ["lft", "rgt"])
			conditions.append("""s.territory in (select name from `tabTerritory`
					where lft>=%s and rgt<=%s and docstatus<2)""" % (lft, rgt))

		if self.filters.get("sales_person"):
			lft, rgt = frappe.db.get_value("Sales Person", self.filters.sales_person, ["lft", "rgt"])
			conditions.append("""sp.sales_person in (select name from `tabSales Person`
					where lft>=%s and rgt<=%s and docstatus<2)""" % (lft, rgt))

		if self.filters.get("order_type"):
			conditions.append("s.order_type=%(order_type)s")

		if self.filters.get("cost_center"):
			self.filters.cost_center = get_cost_centers_with_children(self.filters.get("cost_center"))
			conditions.append("i.cost_center in %(cost_center)s")

		if self.filters.get("project"):
			projects = cstr(self.filters.get("project")).strip()
			self.filters.project = [d.strip() for d in projects.split(',') if d]
			conditions.append(
				"i.project in %(project)s" if frappe.get_meta(self.filters.doctype + " Item").has_field("project")
				else "s.project in %(project)s")

		if self.filters.get("start_month"):
			self.filters.start_month_no = month_to_number[self.filters.start_month]
			conditions.append("MONTH(posting_date) >= %(start_month_no)s")

		if self.filters.get("end_month"):
			self.filters.end_month_no = month_to_number[self.filters.end_month]
			conditions.append("MONTH(posting_date) <= %(end_month_no)s")

		return "and {}".format(" and ".join(conditions)) if conditions else ""

	def get_rows(self):
		self.data = []
		self.get_periodic_data()

		total_row = frappe._dict({"entity": _("'Total'"), "total": 0})
		self.data.append(total_row)

		for entity, period_data in iteritems(self.entity_periodic_data):
			row = {
				"entity": entity,
				"entity_name": self.entity_names.get(entity),
				"indent": 1,
				"total": 0.00
			}
			total = 0
			for end_date in self.periodic_daterange:
				period = self.get_period(end_date)
				sales_person_data = period_data.get(period, frappe._dict())
				for sales_person, amount in sales_person_data.items():
					label, key = self.get_period_label_key(period, sales_person)
					row[key] = amount
					total += amount

					total_row.setdefault(key, 0.0)
					total_row[key] += amount

					total_row["total"] += amount

			row["total"] = total

			if self.filters.tree_type == "Item":
				row["stock_uom"] = period_data.get("stock_uom")

			self.data.append(row)

	def get_rows_by_group(self):
		self.get_periodic_data()
		out = []

		for d in reversed(self.group_entries):
			row = {
				"entity": d.name,
				"indent": self.depth_map.get(d.name)
			}
			total = 0
			for end_date in self.periodic_daterange:
				period = self.get_period(end_date)
				sales_person_data = self.entity_periodic_data.get(d.name, {}).get(period, {})
				if sales_person_data:
					for sales_person, amount in sales_person_data.items():
						label, key = self.get_period_label_key(period, sales_person)
						row[key] = amount
						total += amount
			row["total"] = total
			out = [row] + out
		self.data = out

	def get_periodic_data(self):
		self.entity_periodic_data = frappe._dict()

		for d in self.entries:
			period = self.get_period(d.get(self.date_field))
			sales_person = cstr(d.sales_person)
			for sp in self.sales_persons:
				if cstr(sp.name) == cstr(sales_person): sp.update({'has_entry': True})

			self.entity_periodic_data.setdefault(d.entity, frappe._dict()).setdefault(period, frappe._dict()).setdefault(sales_person, 0.0)
			self.entity_periodic_data[d.entity][period][sales_person] += flt(d.value_field)

			if self.filters.tree_type == "Item":
				self.entity_periodic_data[d.entity]['stock_uom'] = d.stock_uom

	def get_period(self, posting_date):

		if self.filters.range == 'Weekly':
			period = "Week " + str(posting_date.isocalendar()[1]) + " " + str(posting_date.year)
		elif self.filters.range == 'Monthly':
			period = str(self.months[posting_date.month - 1]) + " " + str(posting_date.year)
		elif self.filters.range == 'Quarterly':
			period = "Quarter " + str(((posting_date.month - 1) // 3) + 1) + " " + str(posting_date.year)
		else:
			year = get_fiscal_year(posting_date, company=self.filters.company)
			period = str(year[0])

		return period

	def get_period_date_ranges(self):
		from dateutil.relativedelta import relativedelta, MO
		from_date, to_date = getdate(self.filters.from_date), getdate(self.filters.to_date)

		increment = {
			"Monthly": 1,
			"Quarterly": 3,
			"Half-Yearly": 6,
			"Yearly": 12
		}.get(self.filters.range, 1)

		if self.filters.range in ['Monthly', 'Quarterly']:
			from_date = from_date.replace(day=1)
		elif self.filters.range == "Yearly":
			from_date = get_fiscal_year(from_date)[1]
		else:
			from_date = from_date + relativedelta(from_date, weekday=MO(-1))

		self.periodic_daterange = []
		for dummy in range(1, 53):
			if self.filters.range == "Weekly":
				period_end_date = add_days(from_date, 6)
			else:
				period_end_date = add_to_date(from_date, months=increment, days=-1)

			if period_end_date > to_date:
				period_end_date = to_date

			self.periodic_daterange.append(period_end_date)

			from_date = add_days(period_end_date, 1)
			if period_end_date == to_date:
				break

		self.filters.with_sales_person = self.filters.get('group_by') == 'Sales Person'
		if self.filters.with_sales_person:
			sales_persons = [frappe._dict({"name": None})]
			sales_persons += frappe.get_all("Sales Person", fields=['name'])
			self.sales_persons = sales_persons
		else:
			self.sales_persons = [frappe._dict({"name": None})]

		self.sales_persons = [sp.update({'has_entry': False}) for sp in self.sales_persons]

	def get_groups(self):
		if self.filters.tree_type == "Territory":
			parent = 'parent_territory'
		if self.filters.tree_type == "Customer Group":
			parent = 'parent_customer_group'
		if self.filters.tree_type == "Item Group":
			parent = 'parent_item_group'
		if self.filters.tree_type == "Supplier Group":
			parent = 'parent_supplier_group'
		if self.filters.tree_type == "Sales Person":
			parent = 'parent_sales_person'

		self.depth_map = frappe._dict()

		self.group_entries = frappe.db.sql("""select name, lft, rgt , {parent} as parent
			from `tab{tree}` order by lft"""
		.format(tree=self.filters.tree_type, parent=parent), as_dict=1)

		for d in self.group_entries:
			if d.parent:
				self.depth_map.setdefault(d.name, self.depth_map.get(d.parent) + 1)
			else:
				self.depth_map.setdefault(d.name, 0)

	def get_chart_data(self):
		labels = [d.get("label") for d in self.columns if d.get("period_column")]
		self.chart = {
			"data": {
				'labels': labels,
				'datasets': []
			},
			"type": "line",
			"fieldtype": self.get_value_fieldtype()
		}

		if self.chart.get("fieldtype") == "Currency":
			self.chart['options'] = self.company_currency
