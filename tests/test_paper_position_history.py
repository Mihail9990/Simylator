from copy import deepcopy
from decimal import Decimal
import unittest
from tradfi.paper_position_history import position_history

class PaperPositionHistory(unittest.TestCase):
    def deal(self,id,entry,**kwargs):
        return dict(dict(id=id,position_id='p',entry=entry,side='BUY',volume='.01',price='4000',time=100 if entry=='IN' else 110,reason='EXPERT' if entry=='IN' else 'TP',profit='0' if entry=='IN' else '3',commission='-.1',swap='-.02',fee='-.01'),**kwargs)
    def exchange(self,*rows,live=False):return dict(deals=list(rows),positions=[{'id':'p'}] if live else [],order_history=[{'status':'CANCELLED'}])
    def test_both_costs_and_actual_prices_not_carry(self):
        data=self.exchange(self.deal('i','IN'),self.deal('o','OUT',price='4003'));before=deepcopy(data)
        v=position_history(data,'UST');r=v['position_history'][0]
        self.assertEqual(Decimal(r['net']),Decimal('2.74'));self.assertEqual(r['reason'],'TP');self.assertEqual(r['entry_price'],'4000');self.assertEqual(r['exit_price'],'4003')
        self.assertEqual(v['metrics']['all_closed_net'],'2.74');self.assertTrue(v['metrics']['history_complete']);self.assertEqual(data,before)
    def test_open_commission_excluded_until_closed(self):
        v=position_history(self.exchange(self.deal('i','IN'),live=True),'UST')
        self.assertEqual(v['metrics']['all_closed_net'],'0');self.assertEqual(v['position_history'][0]['status'],'open');self.assertIsNone(v['position_history'][0]['net'])
    def test_sl_profit_and_manual_are_not_relabelled(self):
        for reason,profit in [('SL','2'),('TP','-.01'),('MANUAL','-3'),('PAPER_INTERRUPT','-1')]:
            v=position_history(self.exchange(self.deal('i','IN'),self.deal('o','OUT',reason=reason,profit=profit)),'UST')
            self.assertEqual(v['position_history'][0]['reason'],reason)
            self.assertEqual(Decimal(v['position_history'][0]['net']),Decimal(profit)-Decimal('.26'))
    def test_entire_ledger_not_last_hundred(self):
        rows=[]
        for n in range(120):rows.extend([self.deal('i'+str(n),'IN',position_id=str(n)),self.deal('o'+str(n),'OUT',position_id=str(n))])
        v=position_history(self.exchange(*rows),'UST')
        self.assertEqual(len(v['position_history']),120);self.assertEqual(Decimal(v['metrics']['all_closed_net']),Decimal('2.74')*120)
    def test_duplicate_missing_open_missing_cost_nonfinite_and_volume_fail_closed(self):
        i=self.deal('i','IN');o=self.deal('o','OUT');missing=deepcopy(i);missing.pop('commission')
        for rows in [(i,o,o),(o,),(missing,o),(i,dict(o,profit='NaN')),(i,dict(o,volume='.02'))]:
            v=position_history(self.exchange(*rows),'UST');self.assertIsNone(v['metrics']['all_closed_net']);self.assertFalse(v['metrics']['history_complete'])
    def test_empty_history_cannot_recover_old_completed_cycles(self):
        self.assertIsNone(position_history(self.exchange(),'UST',True)['metrics']['all_closed_net'])
        self.assertEqual(position_history(self.exchange(),'UST',False)['metrics']['all_closed_net'],'0')
    def test_cancel_move_do_not_make_position_history(self):
        v=position_history(self.exchange(),'UST');self.assertEqual(v['position_history'],[])
    def test_unknown_price_time_reason_are_not_invented(self):
        i=self.deal('i','IN');i.pop('price');o=self.deal('o','OUT',time=None,reason=None)
        v=position_history(self.exchange(i,o),'UST');r=v['position_history'][0]
        self.assertIsNone(r['entry_price']);self.assertIsNone(r['closed_at']);self.assertIsNone(r['reason']);self.assertFalse(r['complete']);self.assertIsNone(v['metrics']['all_closed_net'])
    def test_partial_close_excludes_net_until_final_exit(self):
        v=position_history(self.exchange(self.deal('i','IN'),self.deal('o','OUT',volume='.005'),live=True),'UST')
        self.assertEqual(v['position_history'][0]['status'],'partial');self.assertIsNone(v['position_history'][0]['net']);self.assertEqual(v['metrics']['all_closed_net'],'0')
    def test_missing_older_filled_command_is_not_a_complete_aggregate(self):
        data=self.exchange(self.deal('i','IN',command_id='recent'),self.deal('o','OUT'))
        v=position_history(data,'UST',True,{'old':dict(kind='STOP',status='FILLED'),'recent':dict(kind='STOP',status='FILLED')})
        self.assertIsNone(v['metrics']['all_closed_net']);self.assertFalse(v['metrics']['history_complete'])
