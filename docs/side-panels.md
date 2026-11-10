# Chart controls and position history

Two compact arrow handles sit at the lower left edge near the chart and Commands.
The upper handle controls SL; the lower controls manual orders. Both panels slide
from the left independently, without shrinking the chart. Accessible names and
tooltips identify them. Click/Enter/Space toggle; rightward drag of at least 36px
opens, leftward drag closes. Short/vertical/cancelled gestures restore prior state.
Synthetic clicks after dragging are suppressed. Escape, tab changes, modal preview,
blur and resize safely interrupt gestures. Handles never invoke trading actions.

Manual STOP entry and linked-to-SL trigger buttons send guarded requests directly.
Current/next M1 requests still wait for the selected candle. MARKET entries and
protection edits retain confirmation. Linked-to-SL trigger placement, cancellation
and status are in Commands. Cancellation only removes pending triggers; fills keep
protection. Controller identity, revision, safety and duplicate-receipt guards remain.

## Header

RECOVERY is the remaining debt to cover, displayed as a negative amount in lime,
zero when no debt remains. Currency remains UST, with no conversion or dollar sign.
The other value is the net of fully closed positions across the complete saved
execution history, including opening and closing commissions, swap and fees.
Open-position costs are counted only when that position fully closes. Carried debt
and recovery waivers are never P/L and cannot be added again to this total.
Missing or contradictory legacy execution evidence makes the aggregate unavailable
(—) with an explicit note. It is never a guessed total of the last 100 records.

## History

The middle History tab lists positions, not cycle IDs or accounting transfers.
Each record shows BUY/SELL, observed opening/closing time and price, the actual
TP/SL/manual/other reason, and net in UST. TP/SL labels do not imply the sign of net:
a protected SL can be profitable, and fees can make a small TP net negative.
Missing facts remain —. Open/partially closed positions have no final net yet.
Trigger moves/cancellations and recovery writeoffs do not create position rows.
PAPER derives these records from the full persisted exchange deal ledger; neither
history rendering nor totals write to trading state.

The old Paper Trading and Paper tickets/orders sections are removed. Resume after
gap and flatten controls remain under Service controls; modeled costs remain in
Trade parameters. The chart, persisted positions and exchange history are unchanged.

## Notifications and verification

Blue activity messages explicitly describe waiting for candle close, trigger fill,
TP/SL, or a manual order. Fresh snapshots establish position/order waiting states;
safety blocks or pending commands override readiness wording. Warnings and UNKNOWN
outcomes remain visible and never become proof of execution.

Run tools/run_offline_tests.py, tools/*unit.js and
 tools/test_chart_layout_structure.py. The prepared Playwright suite uses mocked
HTTP only. Chromium cannot create sockets in the current environment, including
with approved escalation, so actual browser geometry/touch QA remains unverified.

## Protection drag and flat RECOVERY reset

Hold a TP/SL line still for a full 1000ms before moving it. Movement before that
threshold navigates the chart normally. The scale is frozen for the staged drag;
the chart shows the proposed line but no action is sent. Releasing shows a compact
price/step/ticket confirmation with Confirm and Cancel. Cancelling, pointercancel,
lost capture during drag, multitouch, blur or resize sends nothing. Confirmation
checks fresh scope, cycle, revision and the same position/old levels before using
the original guarded backend action. SL edits still affect the entire basket, as
explicitly stated in the compact confirmation; TP edits target the chosen ticket.

RECOVERY reset is refused while any position is open or an outcome is uncertain.
When flat, it can preserve confirmed pending triggers and a validated scheduled
candle, without cancellation, closing orders, or changing realized P/L. A persistent
waiver offset prevents the old debt from returning; subsequent losses accrue again.
Two fresh snapshots and closed-ticket evidence protect against fills/reconciliation
races. PAPER interruption/manual reconciliation also respect the prior waiver.
