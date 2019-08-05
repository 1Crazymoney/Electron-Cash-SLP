"""
Background search and batch download for graph transactions.

This is used by slp_validator_0x01.py.
"""

import sys
import threading
import queue
import traceback
import weakref
import collections
import json
import base64
import requests
from .transaction import Transaction
from .caches import ExpiringCache

class SlpGraphSearch:
    """
    A single thread that processes graph search requests sequentially.
    """
    def __init__(self, network, wallet, threadname="SlpGraphSearch", errors='print'):
        self.network = network
        self.wallet = wallet
        self.errors = errors

        # status indicators
        self.search_metadata=dict()
        self.job_complete=False
        self.search_success=None
        self.search_error_msg=None
        self.txn_count_total = 0
        self.txn_count_progress = 0

        # search settings
        self.max_txn_dl = 1000

        # Create a single use queue on a new thread
        self.cache_lock = threading.Lock()  # used to lock thread-shared attrs below
        self.graph_search_queue = queue.Queue()
        self.txn_dl_queue = queue.Queue()
        self.thread = threading.Thread(target=self.main, name=threadname, daemon=True)
        self.thread.start()

    @classmethod
    def new_search(self, txids, network, wallet):
        """ start a search job on new thread """
        gs = SlpGraphSearch(network, wallet)
        gs.graph_search_queue.put((list(txids)))
        return gs

    def main(self,):
        try:
            try:
                job = self.graph_search_queue.get()
            except queue.Empty:
                return

            txids = job

            # get max depths using txn's depthMap
            try:
                metadatas = self.metadata_query(txids)
            except Exception as e:
                print("error in graph search query", e, file=sys.stderr)
                self.search_error_msg = str(e)
                return

            # loop through each txid to get txns
            try:
                for item in metadatas:
                    self.txn_count_total+=metadatas[item]['txcount']
                    self.search_query(item, metadatas[item])

            except Exception as e:
                print("error in graph search query", e, file=sys.stderr)
                self.search_error_msg = str(e)
                return
            else:
                self.search_success = True
                print("[SLP Graph Search] job success")
        finally:
            self.job_complete = True
            print("[SLP Graph Search] SearchGraph thread completed.", file=sys.stderr)

    def metadata_query(self, txids):
        if not txids:
            raise RuntimeError("No txids provided for graph search query.")
        requrl = self.metadata_url(txids, self.max_txn_dl)
        print("[SLP Graph Search] depth search url = " + requrl, file=sys.stderr)
        reqresult = requests.get(requrl, timeout=10)
        res = dict()
        for resp in json.loads(reqresult.content.decode('utf-8'))['g']:
            o = { 'queryDepth': resp['queryDepth'], 'txcount': resp['txcount'], 'totalDepth': resp['totalDepth'] }
            res[resp['txid']] = o
            self.search_metadata[resp['txid']] = o
        return res

    def search_query(self, txid, metadata): 
        requrl = self.search_url([txid], metadata['queryDepth']) #TODO: handle 'validity_cache' exclusion from graph search (NOTE: this will impact total dl count)
        print("[SLP Graph Search] txn search url = " + requrl, file=sys.stderr)
        reqresult = requests.get(requrl, timeout=60)
        dependsOn = []
        depths = []
        for resp in json.loads(reqresult.content.decode('utf-8'))['g']:
            dependsOn.extend(resp['dependsOn'])
            depths.extend(resp['depths'])
        txns = [ (d, Transaction(base64.b64decode(tx).hex())) for d,tx in zip(depths, dependsOn) ]
        self.txn_count_progress+=len(txns)
        with self.cache_lock:
            for tx in txns:
                SlpGraphSearch.tx_cache_put(tx[1])
        if metadata['queryDepth'] < metadata['totalDepth']:
            txids = [ tx[1].txid_fast() for tx in txns if tx[0] == metadata['queryDepth'] ]
            metadatas = self.metadata_query(txids)
            for item in metadatas:
                self.txn_count_total+=metadatas[item]['txcount']
                self.search_query(item, metadatas[item])

    def metadata_url(self, txids, txMax=1000):
        txids_q = []
        for txid in txids:
            txids_q.append({"graphTxn.txid": txid})
        q = {
            "v": 3,
            "q": {
                "aggregate": [
                    {"$match": {"$or": txids_q}},
                    {"$project": {
                            "_id": 0,
                            "txid": "$graphTxn.txid",
                            "txcount": "$graphTxn.stats.txcount",
                            "totalDepth": "$graphTxn.stats.depth",
                            "queryDepth": "$graphTxn.stats.depthMap."+str(txMax)
                        }
                    }
                ],
                "limit": len(txids)
            }
        }
        s = json.dumps(q)
        q = base64.b64encode(s.encode('utf-8'))
        if not self.network.slpdb_host:
            print("SLPDB host is not set in network.")
        url = self.network.slpdb_host + "/q/" + q.decode('utf-8')
        return url

    def search_url(self, txids, max_depth, validity_cache=[]):
        print("[SLP Graph Search] " + str(txids))
        txids_q = []
        for txid in txids:
            txids_q.append({"graphTxn.txid": txid})
        q = {
            "v": 3,
            "q": {
                "db": ["g"],
                "aggregate": [
                    {"$match": {"$or": txids_q}},
                    {"$graphLookup": {
                        "from": "graphs",
                        "startWith": "$graphTxn.txid",
                        "connectFromField": "graphTxn.txid",
                        "connectToField": "graphTxn.outputs.spendTxid",
                        "as": "dependsOn",
                        "maxDepth": max_depth,
                        "depthField": "depth",
                        "restrictSearchWithMatch": { #TODO: add tokenId restriction to this for NFT1 application
                            "graphTxn.txid": {"$nin": validity_cache}} 
                    }},
                    {"$project":{
                        "_id":0,
                        "tokenId": "$tokenDetails.tokenIdHex",
                        "txid": "$graphTxn.txid",
                        "dependsOn": {
                            "$map":{
                                "input": "$dependsOn.graphTxn.txid",
                                "in": "$$this"

                            }
                        },
                        "depths": {
                            "$map":{
                                "input": "$dependsOn.depth",
                                "in": "$$this"
                            }
                        }
                        }
                    },
                    {"$unwind": {
                        "path": "$dependsOn", "includeArrayIndex": "depends_index"
                        }
                    },
                    {"$unwind":{
                        "path": "$depths", "includeArrayIndex": "depth_index"
                        }
                    },
                    {"$project": {
                        "tokenId": 1,
                        "txid": 1,
                        "dependsOn": 1,
                        "depths": 1,
                        "compare": {"$cmp":["$depends_index", "$depth_index"]}
                        }
                    },
                    {"$match": {
                        "compare": 0
                        }
                    },
                    {"$group": {
                        "_id":"$dependsOn",
                        "txid": {"$first": "$txid"},
                        "tokenId": {"$first": "$tokenId"},
                        "depths": {"$push": "$depths"}
                        }
                    },
                    {"$lookup": {
                        "from": "confirmed",
                        "localField": "_id",
                        "foreignField": "tx.h",
                        "as": "tx"
                        }
                    },
                    {"$project": {
                        "txid": 1,
                        "tokenId": 1,
                        "depths": 1,
                        "dependsOn": "$tx.tx.raw",
                        "_id": 0
                        }
                    },
                    {
                        "$unwind": "$dependsOn"
                    },
                    {
                        "$unwind": "$depths"
                    },
                    {
                        "$sort": {"depths": 1}
                    },
                    {
                        "$group": {
                            "_id": "$txid",
                            "dependsOn": {"$push": "$dependsOn"},
                            "depths": {"$push": "$depths"},
                            "tokenId": {"$first": "$tokenId"}
                        }
                    },
                    {
                        "$project": {
                            "txid": "$_id",
                            "tokenId": 1,
                            "dependsOn": 1,
                            "depths": 1,
                            "_id": 0, 
                            "txcount": { "$size": "$dependsOn" }
                        }
                    }
                ],
                "limit": len(txids)
            }
            }
        s = json.dumps(q)
        q = base64.b64encode(s.encode('utf-8'))
        if not self.network.slpdb_host:
            raise Exception("SLPDB host is not set in network.")
        url = self.network.slpdb_host + "/q/" + q.decode('utf-8')
        return url

    # This cache stores foreign (non-wallet) tx's we fetched from the network
    # for the purposes of the "fetch_input_data" mechanism. Its max size has
    # been thoughtfully calibrated to provide a decent tradeoff between
    # memory consumption and UX.
    #
    # In even aggressive/pathological cases this cache won't ever exceed
    # 100MB even when full. [see ExpiringCache.size_bytes() to test it].
    # This is acceptable considering this is Python + Qt and it eats memory
    # anyway.. and also this is 2019 ;). Note that all tx's in this cache
    # are in the non-deserialized state (hex encoded bytes only) as a memory
    # savings optimization.  Please maintain that invariant if you modify this
    # code, otherwise the cache may grow to 10x memory consumption if you
    # put deserialized tx's in here.
    _fetched_tx_cache = ExpiringCache(maxlen=1000, name="GraphSearchTxnFetchCache")

    @classmethod
    def tx_cache_get(cls, txid : str) -> object:
        ''' Attempts to retrieve txid from the tx cache that this class
        keeps in-memory.  Returns None on failure. The returned tx is
        not deserialized, and is a copy of the one in the cache. '''
        tx = cls._fetched_tx_cache.get(txid)
        if tx is not None and tx.raw:
            # make sure to return a copy of the transaction from the cache
            # so that if caller does .deserialize(), *his* instance will
            # use up 10x memory consumption, and not the cached instance which
            # should just be an undeserialized raw tx.
            return Transaction(tx.raw)
        return None

    @classmethod
    def tx_cache_put(cls, tx : object, txid : str = None):
        ''' Puts a non-deserialized copy of tx into the tx_cache. '''
        if not tx or not tx.raw:
            raise ValueError('Please pass a tx which has a valid .raw attribute!')
        txid = txid or Transaction._txid(tx.raw)  # optionally, caller can pass-in txid to save CPU time for hashing
        cls._fetched_tx_cache.put(txid, Transaction(tx.raw))