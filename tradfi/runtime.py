"""Simulator-only CLI. This package has no real-order broker or LIVE mode."""
import argparse
import json
from pathlib import Path
from .storage import Store
from .interactive_simulator import InteractiveSimulator


def main(argv=None):
    parser=argparse.ArgumentParser(description='Simylator: offline demo or read-only MT5 PAPER service')
    parser.add_argument('--mode', choices=('simulate','paper'), default='simulate')
    parser.add_argument('--config', default='tradfi_paper_config.json')
    parser.add_argument('--service', action='store_true', help='Compatibility flag; requires --mode paper')
    args=parser.parse_args(argv)
    if args.service and args.mode!='paper':parser.error('--service requires --mode paper')
    if args.mode=='paper':
        from .paper_runtime import main as paper_main
        return paper_main(['--config',args.config])
    if args.mode == 'simulate':
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)/'state.sqlite3')
            try:
                from .recovery_v3 import ControlledRecoveryController
                from .simulator import ARTIFICIAL_SPEC
                broker=InteractiveSimulator(dict(ARTIFICIAL_SPEC,volume_min='0.01',volume_step='0.01'))
                bot=ControlledRecoveryController(store,broker)
                bot.state['tp_distances']=['0.25']+[None]*19
                bot.command('/start');broker.data.update(now=120,candle=dict(id=60,open='3999',close='4000'));bot.tick()
                broker.fill('BUY'); broker.quote('4003', '4003.10')
                for _ in range(4): bot.tick()
                broker.close('TP'); bot.tick()
                print(json.dumps({'mode': 'OFFLINE_SIMULATOR', 'recovery_mode':'V3', 'state': bot.status(),
                                  'completed': bot.state['completed'], 'commands': len(broker.commands)}, indent=2))
            finally:
                store.close()
        return
