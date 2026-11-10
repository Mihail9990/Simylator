"""Read-only MT5 fixture. It has no broker/order/history implementation."""
from collections import namedtuple
from datetime import datetime, timezone


def record(**fields):
    return namedtuple('Record', fields)(**fields)


class UTCAPI:
    TIMEFRAME_M1=1
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2

    def __init__(self):
        self.initialized=self.shutdowns=0
        self.now=datetime(2026,11,2,12,0,30,tzinfo=timezone.utc).timestamp()
        self.shift=0;self.unavailable=False;self.delay=0;self.reads=0
        self.account=record(login=111111111,server='Bybit-Live-6',currency='UST',margin_mode=2)
        self.info=record(visible=True,currency_profit='USD',trade_calc_mode=2,trade_contract_size=100,
            trade_tick_size=.01,trade_tick_value_profit=1,trade_tick_value_loss=1,volume_min=.01,
            volume_step=.01,volume_max=100,point=.01,trade_stops_level=20,trade_freeze_level=0)

    def initialize(self,path,**kwargs):self.initialized+=1;return True
    def shutdown(self):self.shutdowns+=1
    def account_info(self):return self.account
    def symbol_info(self,symbol):return self.info
    def symbol_info_tick(self,symbol):
        self.reads+=1
        if self.unavailable:return None
        raw=self.now+self.shift
        return record(bid=4000.,ask=4000.1,time=int(raw),time_msc=int(raw*1000))
    def copy_rates_from_pos(self,symbol,timeframe,start,count):
        self.reads+=1;self.now+=self.delay
        if self.unavailable:return None
        current=int((self.now+self.shift)//60)*60
        return [dict(time=current-60*i,open=3999.,close=4000.,high=4001.,low=3998.) for i in reversed(range(start,start+count))]
