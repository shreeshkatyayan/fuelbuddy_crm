// Generates so_allocator_cases.json by EXECUTING erp-functions' allocator (IDEV-3266).
// The Python port in fuelbuddy_crm (fuelbuddy_crm/so_allocator.py) replays these cases, so the
// two copies cannot drift. Inputs are hand-written; every `expected` is JavaScript output.
//
//   ERP_FUNCTIONS=<path to erp-functions> node gen_so_allocator_cases.mjs > so_allocator_cases.json
import { execSync } from 'node:child_process'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const root = process.env.ERP_FUNCTIONS
if (!root) throw new Error('set ERP_FUNCTIONS to the erp-functions checkout')
// Module load builds a Frappe client (no network); give it something to hold.
process.env.ERP_ENDPOINT ??= 'http://erp.invalid'
process.env.ERP_TOKEN ??= 'unused'
process.env.ERP_TOKEN_TYPE ??= 'token'

const load = (rel) => import(pathToFileURL(path.join(root, rel)).href)
const { buildSalesOrderQuery, remainingLitres, allocate } = await load('src/erp/allocateSalesOrders.js')
const { reconcileDeliveryNoteLines } = await load('src/erp/reconcileDeliveryNoteLines.js')
const { EPSILON } = await load('src/lib/utils/deliveryNote.util.js')

const blob = (rel) => execSync(`git -C ${root} hash-object ${rel}`).toString().trim()
const commit = execSync(`git -C ${root} rev-parse HEAD`).toString().trim()

// ---- inputs ----------------------------------------------------------------
const soItem = (name, qty, extra = {}) => ({ name, item_code: 'FB/FL/00001', item_name: 'Diesel', uom: 'Litre', conversion_factor: 1, rate: 3.51, price_list_rate: 3.51, discount_percentage: 0, discount_amount: 0, qty, delivered_qty: 0, custom_delivery_note_qty_in_draft: 0, returned_qty: 0, ...extra })
const igItem = (name, qty, extra = {}) => soItem(name, qty, { uom: 'IG', conversion_factor: 4.546, rate: 15.9, price_list_rate: 15.9, ...extra })
const cand = (so, item) => ({ so: { name: so }, soItem: item })
const line = (so, soDetail, qty, extra = {}) => ({ item_code: 'FB/FL/00001', against_sales_order: so, so_detail: soDetail, qty, uom: 'Litre', conversion_factor: 1, rate: 3.51, custom_customer_asset: 'ASSET-1', ...extra })
const igLine = (so, soDetail, qty, extra = {}) => line(so, soDetail, qty, { uom: 'IG', conversion_factor: 4.546, rate: 15.9, ...extra })

const queryCases = [
  { name: 'customer, billing address and posting date', args: ['CUST-00042', 'CUST-00042-Billing', '2026-08-15'] },
  { name: 'first of the month', args: ['Acme Logistics LLC', 'Acme Logistics LLC-Billing-1', '2026-10-01'] }
]

const remainingCases = [
  { name: 'litre line', soItem: { qty: 10000, delivered_qty: 2500, custom_delivery_note_qty_in_draft: 1000, returned_qty: 0, conversion_factor: 1 } },
  { name: 'imperial gallon line converts to litres', soItem: { qty: 1000, delivered_qty: 200, custom_delivery_note_qty_in_draft: 50.5, returned_qty: 10, conversion_factor: 4.546 } },
  { name: 'missing bookkeeping fields count as zero', soItem: { qty: 500 } },
  { name: 'zero conversion factor falls back to 1', soItem: { qty: 300, delivered_qty: 100, conversion_factor: 0 } },
  { name: 'numeric strings, blanks and nulls', soItem: { qty: '1200.5', delivered_qty: '', custom_delivery_note_qty_in_draft: null, returned_qty: '3', conversion_factor: '4.546' } },
  { name: 'over-subscribed line is negative', soItem: { qty: 100, delivered_qty: 80, custom_delivery_note_qty_in_draft: 40, conversion_factor: 1 } },
  { name: 'unparseable values count as zero / factor 1', soItem: { qty: 'abc', delivered_qty: 'x', conversion_factor: 'IG' } },
  { name: 'fractional litres', soItem: { qty: 5000.123, delivered_qty: 1234.567, custom_delivery_note_qty_in_draft: 0.001, returned_qty: 0.5, conversion_factor: 1 } },
  // 400 delivered, then a 100 return submitted: ERPNext shows delivered 300 (net) and returned 100
  { name: 'a return frees its qty once (delivered_qty is already net of it)', soItem: { qty: 10000, delivered_qty: 300, custom_delivery_note_qty_in_draft: 0, returned_qty: 100, conversion_factor: 1 } }
]

const allocateCases = [
  { name: 'fits in the first line', dispensedLitres: 800, candidates: [cand('SO-A', soItem('soi-a', 1000)), cand('SO-B', soItem('soi-b', 1000))] },
  { name: 'spans two lines FIFO', dispensedLitres: 1500, candidates: [cand('SO-A', soItem('soi-a', 1000, { delivered_qty: 300 })), cand('SO-B', soItem('soi-b', 1000))] },
  { name: 'skips an exhausted line', dispensedLitres: 400, candidates: [cand('SO-A', soItem('soi-a', 1000, { delivered_qty: 1000 })), cand('SO-B', soItem('soi-b', 1000, { custom_delivery_note_qty_in_draft: 1200 })), cand('SO-C', soItem('soi-c', 1000))] },
  { name: 'overflow leaves leftover', dispensedLitres: 2600, candidates: [cand('SO-A', soItem('soi-a', 1000)), cand('SO-B', soItem('soi-b', 1000))] },
  { name: 'imperial gallon line capacity in litres', dispensedLitres: 5000, candidates: [cand('SO-IG', igItem('soi-ig', 1000, { delivered_qty: 100 })), cand('SO-L', soItem('soi-l', 10000))] },
  { name: 'nothing to allocate', dispensedLitres: 0, candidates: [cand('SO-A', soItem('soi-a', 1000))] },
  { name: 'dust leftover rounds to zero', dispensedLitres: 1000.0000000001, candidates: [cand('SO-A', soItem('soi-a', 1000))] },
  { name: 'no candidates', dispensedLitres: 50, candidates: [] },
  { name: 'a returned qty is not freed twice', dispensedLitres: 300, candidates: [cand('SO-A', soItem('soi-a', 1000, { delivered_qty: 900, returned_qty: 100 })), cand('SO-B', soItem('soi-b', 1000))] }
]

const reconcileCases = [
  { name: 'unchanged total', existingLines: [line('SO-A', 'soi-a', 1000)], newQtyLitres: 1000, deltaCandidates: [] },
  { name: 'reduction inside the last line', existingLines: [line('SO-A', 'soi-a', 600), line('SO-B', 'soi-b', 400)], newQtyLitres: 850, deltaCandidates: [] },
  { name: 'reduction spans lines, emptied line dropped', existingLines: [line('SO-A', 'soi-a', 600), line('SO-B', 'soi-b', 400)], newQtyLitres: 450, deltaCandidates: [] },
  { name: 'reduction to zero is refused', existingLines: [line('SO-A', 'soi-a', 600), line('SO-B', 'soi-b', 400)], newQtyLitres: 0, deltaCandidates: [] },
  { name: 'imperial gallon last line trimmed in its own uom', existingLines: [line('SO-A', 'soi-a', 500), igLine('SO-IG', 'soi-ig', 100)], newQtyLitres: 800, deltaCandidates: [] },
  { name: 'increase grows the last line within its SO headroom', existingLines: [line('SO-A', 'soi-a', 1000)], newQtyLitres: 1300, deltaCandidates: [cand('SO-A', soItem('soi-a', 5000, { delivered_qty: 1000 }))] },
  { name: 'increase spills onto the next SO', existingLines: [line('SO-A', 'soi-a', 1000)], newQtyLitres: 1800, deltaCandidates: [cand('SO-A', soItem('soi-a', 1500, { delivered_qty: 1000 })), cand('SO-B', soItem('soi-b', 2000))] },
  { name: 'increase past every SO leaves leftover', existingLines: [line('SO-A', 'soi-a', 1000)], newQtyLitres: 2000, deltaCandidates: [cand('SO-A', soItem('soi-a', 1200, { delivered_qty: 1000 })), cand('SO-B', soItem('soi-b', 300))] },
  { name: 'increase skips a full SO', existingLines: [line('SO-A', 'soi-a', 1000)], newQtyLitres: 1100, deltaCandidates: [cand('SO-A', soItem('soi-a', 1000, { delivered_qty: 1000 })), cand('SO-B', soItem('soi-b', 1000))] },
  { name: 'imperial gallon last line grows in gallons, spills onto a litre SO', existingLines: [igLine('SO-IG', 'soi-ig', 100)], newQtyLitres: 1000, deltaCandidates: [cand('SO-IG', igItem('soi-ig', 150, { delivered_qty: 100 })), cand('SO-L', soItem('soi-l', 10000))] },
  { name: 'increase on a line with a return takes the returned qty once, then spills', existingLines: [line('SO-A', 'soi-a', 800)], newQtyLitres: 1100, deltaCandidates: [cand('SO-A', soItem('soi-a', 1000, { delivered_qty: 900, returned_qty: 100 })), cand('SO-B', soItem('soi-b', 1000))] },
  { name: 'spill onto an imperial gallon SO', existingLines: [line('SO-A', 'soi-a', 1000)], newQtyLitres: 1454.6, deltaCandidates: [cand('SO-A', soItem('soi-a', 1000, { delivered_qty: 1000 })), cand('SO-IG', igItem('soi-ig', 500))] },
  { name: 'no lines', existingLines: [], newQtyLitres: 10, deltaCandidates: [] }
]

// ---- execute ----------------------------------------------------------------
const projectLine = (l) => ({ against_sales_order: l.against_sales_order ?? null, so_detail: l.so_detail ?? null, qty: l.qty, uom: l.uom ?? null, conversion_factor: l.conversion_factor ?? null })

const out = {
  _meta: {
    purpose: 'Shared cases for the SO allocator. erp-functions (JS) is the source; fuelbuddy_crm/so_allocator.py (Python) must reproduce every expected value.',
    generated_by: '_shared/gen_so_allocator_cases.mjs (node ' + process.version + ')',
    erp_functions_commit: commit,
    source_blobs: {
      'src/erp/allocateSalesOrders.js': blob('src/erp/allocateSalesOrders.js'),
      'src/erp/reconcileDeliveryNoteLines.js': blob('src/erp/reconcileDeliveryNoteLines.js'),
      'src/erp/createDeliveryNote.js': blob('src/erp/createDeliveryNote.js'),
      'src/lib/utils/deliveryNote.util.js': blob('src/lib/utils/deliveryNote.util.js')
    },
    reconcile_line_projection: ['against_sales_order', 'so_detail', 'qty', 'uom', 'conversion_factor']
  },
  EPSILON,
  buildSalesOrderQuery: queryCases.map(c => ({ ...c, expected: buildSalesOrderQuery(...c.args) })),
  remainingLitres: remainingCases.map(c => ({ ...c, expected: remainingLitres(c.soItem) })),
  allocate: allocateCases.map(c => {
    const r = allocate(c.dispensedLitres, c.candidates)
    return { ...c, expected: { allocations: r.allocations.map(a => ({ so: a.so.name, so_detail: a.soItem.name, allocatedLitres: a.allocatedLitres })), leftover: r.leftover } }
  }),
  reconcileDeliveryNoteLines: reconcileCases.map(c => {
    const r = reconcileDeliveryNoteLines(c.existingLines, c.newQtyLitres, c.deltaCandidates)
    return { ...c, expected: { changed: r.changed, error: r.error ?? null, leftover: r.leftover ?? null, lines: r.lines ? r.lines.map(projectLine) : null } }
  })
}
process.stdout.write(JSON.stringify(out, null, '\t') + '\n')
