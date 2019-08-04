"""
Validate SLP token transactions with declared version 0x01.

This uses the graph searching mechanism from slp_dagging.py
"""

import threading
import queue
import warnings

from .transaction import Transaction
from . import slp
from .slp import SlpMessage, SlpParsingError, SlpUnsupportedSlpTokenType, SlpInvalidOutputMessage
from .slp_dagging import TokenGraph, ValidationJob, ValidationJobManager, ValidatorGeneric
from .bitcoin import TYPE_SCRIPT
from .util import print_error

from . import slp_proxying # loading this module starts a thread.


proxy, config = None, None

def setup_config(config_set):
    """ Called by wallet.py as part of network setup.

    - Limits on downloading DAG.
    - Proxy requests.
    """
    global proxy
    global config

    proxy = slp_proxying.tokengraph_proxy
    config = config_set



class GraphContext:
    ''' Per wallet instance DAG cache '''

    def __init__(self, name='GraphContext'):
        # Global db for shared graphs (each token_id_hex has its own graph).
        self.graph_db_lock = threading.Lock()
        self.graph_db = dict()   # token_id_hex -> TokenGraph
        self.name = name
        self._create_job_mgr()

    def _create_job_mgr(self):
        self.job_mgr = ValidationJobManager(threadname=f'{self.name}/ValidationJobManager')

    def get_graph(self, token_id_hex):
        with self.graph_db_lock:
            try:
                return self.graph_db[token_id_hex]
            except KeyError:
                pass

            val = Validator_SLP1(token_id_hex)

            graph = TokenGraph(val)


            self.graph_db[token_id_hex] = graph

            return graph

    def kill_graph(self, token_id_hex):
        with self.graph_db_lock:
            try:
                graph = self.graph_db.pop(token_id_hex)
            except KeyError:
                return
        #if jobmgr != shared_jobmgr:
        #    jobmgr.kill()
        graph.reset()

    def kill(self):
        with self.graph_db_lock:
            for token_id_hex, graph in self.graph_db.items():
                graph.reset()
            self.graph_db.clear()
        self.job_mgr.kill()
        self._create_job_mgr()  # re-create a new, clean instance

    def setup_job(self, tx, reset=False):
        """ Perform setup steps before validation for a given transaction. """
        slpMsg = SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])

        if slpMsg.transaction_type == 'GENESIS':
            token_id_hex = tx.txid()
        elif slpMsg.transaction_type in ('MINT', 'SEND'):
            token_id_hex = slpMsg.op_return_fields['token_id_hex']
        else:
            return None

        if reset:
            warnings.warn("setup_job with reset is unstable and/or not well specified")
            try:
                self.kill_graph(token_id_hex)
            except KeyError:
                pass

        graph = self.get_graph(token_id_hex)

        return graph


    def make_job(self, tx, wallet, network, *, debug=False, reset=False, callback_done=None, **kwargs):
        """
        Basic validation job maker for a single transaction.

        Creates job and starts it running in the background thread.

        Before calling this you have to call setup_config().

        Returns job, or None if it was not a validatable type
        """
        # This should probably be redone into a class, it is getting messy.

        if config:
            limit_dls   = config.get('slp_validator_download_limit', None)
            limit_depth = config.get('slp_validator_depth_limit', None)
            proxy_enable = config.get('slp_validator_proxy_enabled', False)
        else: # in daemon mode (no GUI) 'config' is not defined
            limit_dls = None
            limit_depth = None
            proxy_enable = False

        try:
            graph = self.setup_job(tx, reset=reset)
        except (SlpParsingError, IndexError):
            return

        txid = tx.txid()

        num_proxy_requests = 0
        proxyqueue = queue.Queue()

        def proxy_cb(txids, results):
            newres = {}
            # convert from 'true/false' to (True,1) or (False,3)
            for t,v in results.items():
                if v:
                    newres[t] = (True, 1)
                else:
                    newres[t] = (True, 3)
            proxyqueue.put(newres)

        def fetch_hook(txids):
            l = []
            for txid in txids:
                try:
                    l.append(wallet.transactions[txid])
                except KeyError:
                    pass
            if proxy_enable:
                proxy.add_job(txids, proxy_cb)
                nonlocal num_proxy_requests
                num_proxy_requests += 1
            return l

        job = ValidationJob(graph, [txid], network,
                            fetch_hook=fetch_hook,
                            validitycache=wallet.slpv1_validity,
                            download_limit=limit_dls,
                            depth_limit=limit_depth,
                            debug=debug,
                            **kwargs)
        def done_callback(job):
            # wait for proxy stuff to roll in
            results = {}
            try:
                for _ in range(num_proxy_requests):
                    r = proxyqueue.get(timeout=5)
                    results.update(r)
            except queue.Empty:
                pass

            if proxy_enable:
                graph.finalize_from_proxy(results)

            # Do consistency check here
            # XXXXXXX

            # Save validity
            for t,n in job.nodes.items():
                val = n.validity
                if val != 0:
                    wallet.slpv1_validity[t] = val
        job.add_callback(done_callback)

        self.job_mgr.add_job(job)

        return job


class Validator_SLP1(ValidatorGeneric):
    prevalidation = True # indicate we want to check validation when some inputs still active.

    validity_states = {
        0: 'Unknown',
        1: 'Valid',
        2: 'Invalid: not SLP / malformed SLP',
        3: 'Invalid: insufficient valid inputs',
        4: 'Invalid: token type different than required'
        }

    def __init__(self, token_id_hex, *, enforced_token_type=1):
        self.token_id_hex = token_id_hex
        self.token_type = enforced_token_type

    def get_info(self,tx):
        """
        Enforce internal consensus rules (check all rules that don't involve
        information from inputs).

        Prune if mismatched token_id_hex from this validator or SLP version other than 1.
        """
        txouts = tx.outputs()
        if len(txouts) < 1:
            return ('prune', 2) # not SLP -- no outputs!

        # We take for granted that parseSlpOutputScript here will catch all
        # consensus-invalid op_return messages. In this procedure we check the
        # remaining internal rules, having to do with the overall transaction.
        try:
            slpMsg = SlpMessage.parseSlpOutputScript(txouts[0][1])
        except SlpUnsupportedSlpTokenType as e:
            # for unknown types: pruning as unknown has similar effect as pruning
            # invalid except it tells the validity cacher to not remember this
            # tx as 'bad'
            return ('prune', 0)
        except SlpInvalidOutputMessage as e:
            return ('prune', 2)

        # Parse the SLP
        if slpMsg.token_type not in [1,129]:
            return ('prune', 0)

        # Check that the correct token_type is enforced (type 0x01 or 0x81)
        if self.token_type != slpMsg.token_type:
            return ('prune', 4)

        if slpMsg.transaction_type == 'SEND':
            token_id_hex = slpMsg.op_return_fields['token_id_hex']

            # need to examine all inputs
            vin_mask = (True,)*len(tx.inputs())

            # myinfo is the output sum
            # Note: according to consensus rules, we compute sum before truncating extra outputs.
#            print("DEBUG SLP:getinfo %.10s outputs: %r"%(tx.txid(), slpMsg.op_return_fields['token_output']))
            myinfo = sum(slpMsg.op_return_fields['token_output'])

            # outputs straight from the token amounts
            outputs = slpMsg.op_return_fields['token_output']
        elif slpMsg.transaction_type == 'GENESIS':
            token_id_hex = tx.txid()

            vin_mask = (False,)*len(tx.inputs()) # don't need to examine any inputs.

            myinfo = 'GENESIS'

            # place 'MINT' as baton signifier on the designated output
            mintvout = slpMsg.op_return_fields['mint_baton_vout']
            if mintvout is None:
                outputs = [None,None]
            else:
                outputs = [None]*(mintvout) + ['MINT']
            outputs[1] = slpMsg.op_return_fields['initial_token_mint_quantity']
        elif slpMsg.transaction_type == 'MINT':
            token_id_hex = slpMsg.op_return_fields['token_id_hex']

            vin_mask = (True,)*len(tx.inputs()) # need to examine all vins, even for baton.

            myinfo = 'MINT'

            # place 'MINT' as baton signifier on the designated output
            mintvout = slpMsg.op_return_fields['mint_baton_vout']
            if mintvout is None:
                outputs = [None,None]
            else:
                outputs = [None]*(mintvout) + ['MINT']
            outputs[1] = slpMsg.op_return_fields['additional_token_quantity']
        elif slpMsg.transaction_type == 'COMMIT':
            return ('prune', 0)

        if token_id_hex != self.token_id_hex:
            return ('prune', 0)  # mismatched token_id_hex

        # truncate / expand outputs list to match tx outputs length
        outputs = tuple(outputs[:len(txouts)])
        outputs = outputs + (None,)*(len(txouts) - len(outputs))

        return vin_mask, myinfo, outputs


    def check_needed(self, myinfo, out_n):
        if myinfo == 'MINT':
            # mints are only interested in the baton input
            return (out_n == 'MINT')
        if myinfo == 'GENESIS':
            # genesis shouldn't have any parents, so this should not happen.
            raise RuntimeError('Unexpected', out_n)

        # TRAN txes are only interested in integer, non-zero input contributions.
        if out_n is None or out_n == 'MINT':
            return False
        else:
            return (out_n > 0)


    def validate(self, myinfo, inputs_info):
        if myinfo == 'GENESIS':
            if len(inputs_info) != 0:
                raise RuntimeError('Unexpected', inputs_info)
            return (True, 1)   # genesis is always valid.
        elif myinfo == 'MINT':
            if not all(inp[2] == 'MINT' for inp in inputs_info):
                raise RuntimeError('non-MINT inputs should have been pruned!', inputs_info)
            if len(inputs_info) == 0:
                return (False, 3) # no baton? invalid.
            if any(inp[1] == 1 for inp in inputs_info):
                # Why we use 'any' here:
                # multiple 'valid' baton inputs are possible with double spending.
                # technically 'valid' though miners will never confirm.
                return (True, 1)
            return None
        else:
            # TRAN --- myinfo is an integer sum(outs)

            # Check whether from the unknown + valid inputs there could be enough to satisfy outputs.
            insum_all = sum(inp[2] for inp in inputs_info if inp[1] <= 1)
            if insum_all < myinfo:
                return (False, 3)

            # Check whether the known valid inputs provide enough tokens to satisfy outputs:
            insum_valid = sum(inp[2] for inp in inputs_info if inp[1] == 1)
            if insum_valid >= myinfo:
                return (True, 1)
            return None
