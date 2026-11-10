# Independent side panels

The SL controls slide from the left; manual order controls slide from the right.
They open separately without expanding Commands or shrinking the chart. Close
buttons and Escape dismiss one panel; switching to Settings closes both.

Manual STOP and SL-linked trigger buttons submit the guarded request directly,
without a preview confirmation. Current/next M1 scheduling still means the
orders are placed only when that candle closes. MARKET entries and edits to
existing protections retain confirmation.

Cancel waiting candle, cancel placed manual triggers, and cancel SL-linked
triggers are distinct actions. Cancelling triggers never closes filled positions.
The controller records cancellation before dispatch, reconciles after restart,
and preserves filled profile stages and their protection. UNKNOWN outcomes block
retries pending reconciliation. Read-only, snapshot revision and cycle guards
remain enforced.

Status colors: blue for information, green for a controller-accepted command
(not proof of execution), amber for warnings, purple for uncertain/pending
outcomes. The duplicated raw error banner is removed; readable safety warnings
remain at the top, while exact controller codes remain in the Settings journal.

Validation: tools/run_offline_tests.py, tools/*unit.js and
 tools/test_chart_layout_structure.py. tools/test_side_panels_ui.js is an offline
mocked Playwright test for 320/390/1280px widths, independent drawers,
Escape/Settings, direct submit/cancel, notices and landscape. Chromium launch is
blocked in the current execution environment by a socket permission restriction;
browser geometry and touch behavior have not been verified here.

## Header totals and History

The fixed header shows total outstanding RECOVERY (lime digits) and the active
cycle's booked net. Total RECOVERY is the existing cycle recovery or basket carry.
Current cycle net sums confirmed ticket opening costs and closed ticket exit net,
excluding inherited carry and unrealized floating P/L. Missing legacy ticket
amounts or absence of an active cycle display as unknown (—), not fabricated zero.

History is the middle bottom tab. It contains the last 100 stored cycle summaries,
existing paper executions, and existing writeoffs. Historical `realized` is labeled
«Учёт с переносом» because it includes carried recovery; old records do not become
reconstructed per-cycle returns. Backend projections are read-only. Tabs support
left/right arrows; opening History closes side panels, and modal preview disables
History interactions too. No historical timestamps or values are invented.
