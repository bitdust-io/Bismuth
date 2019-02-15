# c = hyperblock in ram OR hyperblock file when running only hyperblocks
# h = ledger file
# h2 = hyperblock file
# h3 = ledger file OR hyperblock file when running only hyperblocks

# never remove the str() conversion in data evaluation or database inserts or you will debug for 14 days as signed types mismatch
# if you raise in the server thread, the server will die and node will stop
# never use codecs, they are bugged and do not provide proper serialization
# must unify node and client now that connections parameters are function parameters
# if you have a block of data and want to insert it into sqlite, you must use a single "commit" for the whole batch, it's 100x faster
# do not isolation_level=None/WAL hdd levels, it makes saving slow
# issues with db? perhaps you missed a commit() or two


app_version = "4.2.9.2"
VERSION = app_version #compat hypernodes

import base64
import glob
import hashlib
import math
import queue
import re
import shutil
import socketserver
import sqlite3
import tarfile
import threading
import time
from hashlib import blake2b

import requests
import socks
from Cryptodome.Hash import SHA
from Cryptodome.PublicKey import RSA
from Cryptodome.Signature import PKCS1_v1_5

import aliases
# Bis specific modules
import apihandler
import classes

from connections import send, receive

import dbhandler
import essentials
import keys
import log
import mempool as mp
import mining
import mining_heavy3
import options
import peershandler
import plugins
import regnet
import staking
import tokensv2 as tokens
from essentials import fee_calculate, db_to_drive, ledger_balance3, checkpoint_set
from quantizer import *
import connectionmanager
from difficulty import *
from fork import *
from digest import *

# load config



getcontext().rounding = ROUND_HALF_EVEN
POW_FORK, FORK_AHEAD, FORK_DIFF = fork()


from appdirs import *

appname = "Bismuth"
appauthor = "Bismuth Foundation"

# nodes_ban_reset=config.nodes_ban_reset

PEM_BEGIN = re.compile(r"\s*-----BEGIN (.*)-----\s+")
PEM_END = re.compile(r"-----END (.*)-----\s*$")


def replace_regex(string, replace):
    replaced_string = re.sub(r'^{}'.format(replace), "", string)
    return replaced_string

def tokens_rollback(node, height, db_handler):
    """Rollback Token index

    :param height: height index of token in chain

    Simply deletes from the `tokens` table where the block_height is
    greater than or equal to the :param height: and logs the new height

    returns None
    """
    try:
        db_handler.execute_param(db_handler.index_cursor, "DELETE FROM tokens WHERE block_height >= ?;", (height,))
        db_handler.commit(db_handler.index)

        node.logger.app_log.warning(f"Rolled back the token index below {(height)}")
    except Exception as e:
        node.logger.app_log.warning(f"Failed to roll back the token index below {(height)} due to {e}")


def staking_rollback(node, height, db_handler):
    """Rollback staking index

    :param height: height index of token in chain

    Simply deletes from the `staking` table where the block_height is
    greater than or equal to the :param height: and logs the new height

    returns None
    """
    try:
        db_handler.execute_param(db_handler.index_cursor, "DELETE FROM staking WHERE block_height >= ?;", (height,))
        db_handler.commit(db_handler.index)

        node.logger.app_log.warning(f"Rolled back the staking index below {(height)}")
    except Exception as e:
        node.logger.app_log.warning(f"Failed to roll back the staking index below {(height)} due to {e}")


def aliases_rollback(node, height, db_handler):
    """Rollback Alias index

    :param height: height index of token in chain

    Simply deletes from the `aliases` table where the block_height is
    greater than or equal to the :param height: and logs the new height

    returns None
    """
    try:
        db_handler.execute_param(db_handler.index_cursor, "DELETE FROM aliases WHERE block_height >= ?;", (height,))
        db_handler.commit(db_handler.index)

        node.logger.app_log.warning(f"Rolled back the alias index below {(height)}")
    except Exception as e:
        node.logger.app_log.warning(f"Failed to roll back the alias index below {(height)} due to {e}")




def validate_pem(public_key):
    """ Validate PEM data against :param public key:

    :param public_key: public key to validate PEM against

    The PEM data is constructed by base64 decoding the public key
    Then, the data is tested against the PEM_BEGIN and PEM_END
    to ensure the `pem_data` is valid, thus validating the public key.

    returns None
    """
    # verify pem as cryptodome does
    pem_data = base64.b64decode(public_key).decode("utf-8")
    match = PEM_BEGIN.match(pem_data)
    if not match:
        raise ValueError("Not a valid PEM pre boundary")

    marker = match.group(1)

    match = PEM_END.search(pem_data)
    if not match or match.group(1) != marker:
        raise ValueError("Not a valid PEM post boundary")
        # verify pem as cryptodome does


def download_file(url, filename):
    """Download a file from URL to filename

    :param url: URL to download file from
    :param filename: Filename to save downloaded data as

    returns `filename`
    """
    try:
        r = requests.get(url, stream=True)
        total_size = int(r.headers.get('content-length')) / 1024

        with open(filename, 'wb') as filename:
            chunkno = 0
            for chunk in r.iter_content(chunk_size=1024):
                if chunk:
                    chunkno = chunkno + 1
                    if chunkno % 10000 == 0:  # every x chunks
                        print(f"Downloaded {int(100 * (chunkno / total_size))} %")

                    filename.write(chunk)
                    filename.flush()
            print("Downloaded 100 %")

        return filename
    except:
        raise


# load config

def most_common(lst):
    return max(set(lst), key=lst.count)


def bootstrap():
    try:
        types = ['static/*.db-wal', 'static/*.db-shm']
        for t in types:
            for f in glob.glob(t):
                os.remove(f)
                print(f, "deleted")

        archive_path = node.ledger_path_conf + ".tar.gz"
        download_file("https://bismuth.cz/ledger.tar.gz", archive_path)

        with tarfile.open(archive_path) as tar:
            tar.extractall("static/")  # NOT COMPATIBLE WITH CUSTOM PATH CONFS

    except:
        node.logger.app_log.warning("Something went wrong during bootstrapping, aborted")
        raise


def check_integrity(database):
    # check ledger integrity
    logger.app_log.warning(f"Status: check_integrity method!!!")
    with sqlite3.connect(database) as ledger_check:
        ledger_check.text_factory = str
        l = ledger_check.cursor()

        try:
            l.execute("PRAGMA table_info('transactions')")
            redownload = False
        except:
            redownload = True

        if len(l.fetchall()) != 12:
            node.logger.app_log.warning(
                f"Status: Integrity check on database {database} failed, bootstrapping from the website")
            redownload = True

    if redownload and node.is_mainnet:
        logger.app_log.warning(f"Status: BOOTSTRAP disabled")
        bootstrap()


def percentage(percent, whole):
    return Decimal(percent) * Decimal(whole) / 100

def rollback_to(node, db_handler, block_height):
    node.logger.app_log.warning(f"Status: Rolling back below: {block_height}")

    db_handler.h.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (block_height,-block_height,))
    db_handler.commit(db_handler.hdd)
    db_handler.h.execute("DELETE FROM misc WHERE block_height >= ?", (block_height,))
    db_handler.commit(db_handler.hdd)

    db_handler.h2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (block_height, -block_height,))
    db_handler.commit(db_handler.hdd2)
    db_handler.h2.execute("DELETE FROM misc WHERE block_height >= ?", (block_height,))
    db_handler.commit(db_handler.hdd2)

    # rollback indices
    tokens_rollback(node, block_height, db_handler)
    aliases_rollback(node, block_height, db_handler)
    staking_rollback(node, block_height, db_handler)
    # rollback indices

    node.logger.app_log.warning(f"Status: Chain rolled back below {block_height} and will be resynchronized")


def recompress_ledger(node, rebuild=False, depth=15000):

    files_remove = [node.ledger_path_conf + '.temp',node.ledger_path_conf + '.temp-shm',node.ledger_path_conf + '.temp-wal']
    for file in files_remove:
        if os.path.exists(file):
            os.remove(file)
            node.logger.app_log.warning(f"Removed old {file}")

    if node.full_ledger and rebuild:
        node.logger.app_log.warning(f"Status: Hyperblocks will be rebuilt")

        shutil.copy(node.ledger_path_conf, node.ledger_path_conf + '.temp')
        hyper = sqlite3.connect(node.ledger_path_conf + '.temp')
    else:
        shutil.copy(node.hyper_path_conf, node.ledger_path_conf + '.temp')
        hyper = sqlite3.connect(node.ledger_path_conf + '.temp')

    hyper.text_factory = str
    hyp = hyper.cursor()

    hyp.execute("UPDATE transactions SET address = 'Hypoblock' WHERE address = 'Hyperblock'")

    hyp.execute("SELECT max(block_height) FROM transactions")
    db_block_height = int(hyp.fetchone()[0])
    depth_specific = db_block_height - depth

    hyp.execute(
        "SELECT distinct(recipient) FROM transactions WHERE (block_height < ? AND block_height > ?) ORDER BY block_height;",
        (depth_specific, -depth_specific,))  # new addresses will be ignored until depth passed
    unique_addressess = hyp.fetchall()

    for x in set(unique_addressess):
        credit = Decimal("0")
        for entry in hyp.execute(
                "SELECT amount,reward FROM transactions WHERE recipient = ? AND (block_height < ? AND block_height > ?);",
                (x[0],) + (depth_specific, -depth_specific,)):
            try:
                credit = quantize_eight(credit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                credit = 0 if credit is None else credit
            except Exception:
                credit = 0

        debit = Decimal("0")
        for entry in hyp.execute(
                "SELECT amount,fee FROM transactions WHERE address = ? AND (block_height < ? AND block_height > ?);",
                (x[0],) + (depth_specific, -depth_specific,)):
            try:
                debit = quantize_eight(debit) + quantize_eight(entry[0]) + quantize_eight(entry[1])
                debit = 0 if debit is None else debit
            except Exception:
                debit = 0

        end_balance = quantize_eight(credit - debit)

        if end_balance > 0:
            timestamp = str(time.time())
            hyp.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                depth_specific - 1, timestamp, "Hyperblock", x[0], str(end_balance), "0", "0", "0", "0",
                "0", "0", "0"))
    hyper.commit()

    hyp.execute(
        "DELETE FROM transactions WHERE address != 'Hyperblock' AND (block_height < ? AND block_height > ?);",
        (depth_specific, -depth_specific,))
    hyper.commit()

    hyp.execute("DELETE FROM misc WHERE (block_height < ? AND block_height > ?);",
                (depth_specific, -depth_specific,))  # remove diff calc
    hyper.commit()

    hyp.execute("VACUUM")
    hyper.close()


    if os.path.exists(node.hyper_path_conf) and rebuild:
        os.remove(node.hyper_path_conf)  # remove the old hyperblocks to rebuild
        os.rename(node.ledger_path_conf + '.temp', node.hyper_path_conf)

    if node.full_ledger == 0 and os.path.exists(node.ledger_path_conf) and node.is_mainnet:
        os.remove(node.ledger_path_conf)
        node.logger.app_log.warning("Removed full ledger and only kept hyperblocks")




def ledger_check_heights(node, db_handler):
    """conversion of normal blocks into hyperblocks from ledger.db or hyper.db to hyper.db"""

    if os.path.exists(node.hyper_path_conf):

        if node.full_ledger:
            # cross-integrity check
            db_handler.h.execute("SELECT max(block_height) FROM transactions")
            hdd_block_last = db_handler.h.fetchone()[0]
            db_handler.h.execute("SELECT max(block_height) FROM misc")
            hdd_block_last_misc = db_handler.h.fetchone()[0]

            db_handler.h2.execute("SELECT max(block_height) FROM transactions")
            hdd2_block_last = db_handler.h2.fetchone()[0]
            db_handler.h2.execute("SELECT max(block_height) FROM misc")
            hdd2_block_last_misc = db_handler.h2.fetchone()[0]
            # cross-integrity check

            if hdd_block_last == hdd2_block_last == hdd2_block_last_misc == hdd_block_last_misc and node.hyper_recompress_conf:  # cross-integrity check
                node.logger.app_log.warning("Status: Recompressing hyperblocks (keeping full ledger)")
                recompress = True
            elif hdd_block_last == hdd2_block_last and not node.hyper_recompress_conf:
                node.logger.app_log.warning("Status: Hyperblock recompression skipped")
                recompress = False
            else:
                lowest_block = min(hdd_block_last, hdd2_block_last, hdd_block_last_misc, hdd2_block_last_misc)
                highest_block = max(hdd_block_last, hdd2_block_last, hdd_block_last_misc, hdd2_block_last_misc)


                node.logger.app_log.warning(
                    f"Status: Cross-integrity check failed, {highest_block} will be rolled back below {lowest_block}")

                rollback_to(node,db_handler_initial,lowest_block) #rollback to the lowest value

                recompress = True
        else:
            if node.hyper_recompress_conf:
                node.logger.app_log.warning("Status: Recompressing hyperblocks (without full ledger)")
                recompress = True
            else:
                node.logger.app_log.warning("Status: Hyperblock recompression skipped")
                recompress = False

    else:
        node.logger.app_log.warning("Status: Compressing ledger to Hyperblocks")
        recompress = True

    if recompress:
        recompress_ledger(node)





def most_common(lst):
    return max(set(lst), key=lst.count)


def bin_convert(string):
    return ''.join(format(ord(x), '8b').replace(' ', '0') for x in string)




def balanceget(balance_address, db_handler):
    # verify balance

    # node.logger.app_log.info("Mempool: Verifying balance")
    # node.logger.app_log.info("Mempool: Received address: " + str(balance_address))

    base_mempool = mp.MEMPOOL.fetchall("SELECT amount, openfield, operation FROM transactions WHERE address = ?;",
                                       (balance_address,))

    # include mempool fees

    debit_mempool = 0
    if base_mempool:
        for x in base_mempool:
            debit_tx = Decimal(x[0])
            fee = fee_calculate(x[1], x[2], node.last_block)
            debit_mempool = quantize_eight(debit_mempool + debit_tx + fee)
    else:
        debit_mempool = 0
    # include mempool fees

    credit_ledger = Decimal("0")

    try:
        db_handler.execute_param(db_handler.h3, "SELECT amount FROM transactions WHERE recipient = ?;", (balance_address,))
        entries = db_handler.h3.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            credit_ledger = quantize_eight(credit_ledger) + quantize_eight(entry[0])
            credit_ledger = 0 if credit_ledger is None else credit_ledger
    except:
        credit_ledger = 0

    fees = Decimal("0")
    debit_ledger = Decimal("0")

    try:
        db_handler.execute_param(db_handler.h3, "SELECT fee, amount FROM transactions WHERE address = ?;", (balance_address,))
        entries = db_handler.h3.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            fees = quantize_eight(fees) + quantize_eight(entry[0])
            fees = 0 if fees is None else fees
    except:
        fees = 0

    try:
        for entry in entries:
            debit_ledger = debit_ledger + Decimal(entry[1])
            debit_ledger = 0 if debit_ledger is None else debit_ledger
    except:
        debit_ledger = 0

    debit = quantize_eight(debit_ledger + debit_mempool)

    rewards = Decimal("0")

    try:
        db_handler.execute_param(db_handler.h3, "SELECT reward FROM transactions WHERE recipient = ?;", (balance_address,))
        entries = db_handler.c.fetchall()
    except:
        entries = []

    try:
        for entry in entries:
            rewards = quantize_eight(rewards) + quantize_eight(entry[0])
            rewards = 0 if rewards is None else rewards
    except:
        rewards = 0

    balance = quantize_eight(credit_ledger - debit - fees + rewards)
    balance_no_mempool = float(credit_ledger) - float(debit_ledger) - float(fees) + float(rewards)
    # node.logger.app_log.info("Mempool: Projected transction address balance: " + str(balance))
    return str(balance), str(credit_ledger), str(debit), str(fees), str(rewards), str(balance_no_mempool)


def blocknf(node, block_hash_delete, peer_ip, db_handler):
    node.logger.app_log.info(f"Rollback operation on {block_hash_delete} initiated by {peer_ip}")

    my_time = time.time()

    if not node.db_lock.locked():
        node.db_lock.acquire()
        backup_data = None  # used in "finally" section
        skip = False
        reason = ""

        try:
            db_handler.execute(db_handler.c, 'SELECT * FROM transactions ORDER BY block_height DESC LIMIT 1')
            results = db_handler.c.fetchone()
            db_block_height = results[0]
            db_block_hash = results[7]

            ip = {'ip': peer_ip}
            node.plugin_manager.execute_filter_hook('filter_rollback_ip', ip)
            if ip['ip'] == 'no':
                reason = "Filter blocked this rollback"
                skip = True

            elif db_block_height < node.checkpoint:
                reason = "Block is past checkpoint, will not be rolled back"
                skip = True

            elif db_block_hash != block_hash_delete:
                # print db_block_hash
                # print block_hash_delete
                reason = "We moved away from the block to rollback, skipping"
                skip = True

            else:
                db_handler.execute_param(db_handler.c, "SELECT * FROM transactions WHERE block_height >= ?;", (db_block_height,))
                backup_data = db_handler.c.fetchall()
                # this code continues at the bottom because of ledger presence check

                # delete followups
                db_handler.execute_param(db_handler.c, "DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                                         (db_block_height, -db_block_height))
                db_handler.commit(db_handler.conn)

                db_handler.execute_param(db_handler.c, "DELETE FROM misc WHERE block_height >= ?;", (db_block_height,))
                db_handler.commit(db_handler.conn)

                node.logger.app_log.warning(f"Node {peer_ip} didn't find block {db_block_height}({db_block_hash})")

                # roll back hdd too
                if node.full_ledger:  # rollback ledger.db
                    db_handler.execute_param(db_handler.h, "DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                                             (db_block_height, -db_block_height))
                    db_handler.commit(db_handler.hdd)
                    db_handler.execute_param(db_handler.h, "DELETE FROM misc WHERE block_height >= ?;", (db_block_height,))
                    db_handler.commit(db_handler.hdd)

                if node.ram_conf:  # rollback hyper.db
                    db_handler.execute_param(db_handler.h2, "DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?",
                                             (db_block_height, -db_block_height))
                    db_handler.commit(db_handler.hdd2)
                    db_handler.execute_param(db_handler.h2, "DELETE FROM misc WHERE block_height >= ?;", (db_block_height,))
                    db_handler.commit(db_handler.hdd2)

                node.hdd_block = db_block_height - 1
                # /roll back hdd too

                # rollback indices
                tokens_rollback(node, db_block_height, db_handler)
                aliases_rollback(node, db_block_height, db_handler)
                staking_rollback(node, db_block_height, db_handler)
                # /rollback indices

        except Exception as e:
            node.logger.app_log.warning(e)


        finally:
            node.db_lock.release()
            if skip:
                rollback = {"timestamp": my_time, "height": db_block_height, "ip": peer_ip,
                            "sha_hash": db_block_hash, "skipped": True, "reason": reason}
                node.plugin_manager.execute_action_hook('rollback', rollback)
                node.logger.app_log.info(f"Skipping rollback: {reason}")
            else:
                try:
                    nb_tx = 0
                    for tx in backup_data:
                        tx_short = f"{tx[1]} - {tx[2]} to {tx[3]}: {tx[4]} ({tx[11]})"
                        if tx[9] == 0:
                            try:
                                nb_tx += 1
                                node.logger.app_log.info(
                                    mp.MEMPOOL.merge((tx[1], tx[2], tx[3], tx[4], tx[5], tx[6], tx[10], tx[11]),
                                                     peer_ip, db_handler.c, False, revert=True))  # will get stuck if you change it to respect node.db_lock
                                node.logger.app_log.warning(f"Moved tx back to mempool: {tx_short}")
                            except Exception as e:
                                node.logger.app_log.warning(f"Error during moving tx back to mempool: {e}")
                        else:
                            # It's the coinbase tx, so we get the miner address
                            miner = tx[3]
                            height = tx[0]
                    rollback = {"timestamp": my_time, "height": height, "ip": peer_ip, "miner": miner,
                                "sha_hash": db_block_hash, "tx_count": nb_tx, "skipped": False, "reason": ""}
                    node.plugin_manager.execute_action_hook('rollback', rollback)

                except Exception as e:
                    node.logger.app_log.warning(f"Error during moving txs back to mempool: {e}")

    else:
        reason = "Skipping rollback, other ledger operation in progress"
        rollback = {"timestamp": my_time, "ip": peer_ip, "skipped": True, "reason": reason}
        node.plugin_manager.execute_action_hook('rollback', rollback)
        node.logger.app_log.info(reason)

def sequencing_check(db_handler):
    try:
        with open("sequencing_last", 'r') as filename:
            sequencing_last = int(filename.read())

    except:
        node.logger.app_log.warning("Sequencing anchor not found, going through the whole chain")
        sequencing_last = 0

    node.logger.app_log.warning(f"Status: Testing chain sequencing, starting with block {sequencing_last}")

    if node.full_ledger:
        chains_to_check = [node.ledger_path_conf, node.hyper_path_conf]
    else:
        chains_to_check = [node.hyper_path_conf]

    for chain in chains_to_check:
        conn = sqlite3.connect(chain)
        c = conn.cursor()

        # perform test on transaction table
        y = None
        # Egg: not sure block_height != (0 OR 1)  gives the proper result, 0 or 1  = 1. not in (0, 1) could be better.
        for row in c.execute(
                "SELECT block_height FROM transactions WHERE reward != 0 AND block_height > 1 AND block_height >= ? ORDER BY block_height ASC",
                (sequencing_last,)):
            y_init = row[0]

            if y is None:
                y = y_init

            if row[0] != y:

                for chain2 in chains_to_check:
                    conn2 = sqlite3.connect(chain2)
                    c2 = conn2.cursor()
                    node.logger.app_log.warning(f"Status: Chain {chain} transaction sequencing error at: {row[0]}. {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ? OR block_height <= ?", (row[0], -row[0],))
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", (row[0],))
                    conn2.commit()

                    # rollback indices
                    tokens_rollback(node, y, db_handler)
                    aliases_rollback(node, y, db_handler)
                    staking_rollback(node, y, db_handler)

                    # rollback indices

                    node.logger.app_log.warning(f"Status: Due to a sequencing issue at block {y}, {chain} has been rolled back and will be resynchronized")
                break

            y = y + 1

        # perform test on misc table
        y = None

        for row in c.execute("SELECT block_height FROM misc WHERE block_height > ? ORDER BY block_height ASC",
                             (300000,)):
            y_init = row[0]

            if y is None:
                y = y_init
                # print("assigned")
                # print(row[0], y)

            if row[0] != y:
                # print(row[0], y)
                for chain2 in chains_to_check:
                    conn2 = sqlite3.connect(chain2)
                    c2 = conn2.cursor()
                    node.logger.app_log.warning(
                        f"Status: Chain {chain} difficulty sequencing error at: {row[0]} {row[0]} instead of {y}")
                    c2.execute("DELETE FROM transactions WHERE block_height >= ?", row[0],)
                    conn2.commit()
                    c2.execute("DELETE FROM misc WHERE block_height >= ?", row[0],)
                    conn2.commit()

                    db_handler.execute_param(conn2, (
                        'DELETE FROM transactions WHERE address = "Development Reward" AND block_height <= ?'),
                                             (-row[0],))
                    conn2.commit()
                    conn2.close()

                    # rollback indices
                    tokens_rollback(node, y, db_handler)
                    aliases_rollback(node, y, db_handler)
                    staking_rollback(node, y, db_handler)
                    # rollback indices

                    node.logger.app_log.warning(f"Status: Due to a sequencing issue at block {y}, {chain} has been rolled back and will be resynchronized")
                break

            y = y + 1

        node.logger.app_log.warning(f"Status: Chain sequencing test complete for {chain}")
        conn.close()

        if y:
            with open("sequencing_last", 'w') as filename:
                filename.write(str(y - 1000))  # room for rollbacks


# init

class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
    def handle(self):
        if node.IS_STOPPING:
            return

        client_instance = classes.Client()
        db_handler_instance = dbhandler.DbHandler(node.index_db, node.ledger_path_conf, node.hyper_path_conf, node.full_ledger, node.ram_conf, node.ledger_ram_file, node.logger)

        try:
            peer_ip = self.request.getpeername()[0]
        except:
            node.logger.app_log.warning("Inbound: Transport endpoint was not connected")
            return

        # if threading.active_count() < node.thread_limit_conf or peer_ip == "127.0.0.1":
        # Always keep a slot for whitelisted (wallet could be there)
        if threading.active_count() < node.thread_limit_conf / 3 * 2 or node.peers.is_whitelisted(peer_ip):  # inbound
            client_instance.connected = True
        else:
            try:
                self.request.close()
                node.logger.app_log.info(f"Free capacity for {peer_ip} unavailable, disconnected")
                # if you raise here, you kill the whole server
            except:
                pass
            finally:
                return

        dict_ip = {'ip': peer_ip}
        node.plugin_manager.execute_filter_hook('peer_ip', dict_ip)
        if node.peers.is_banned(peer_ip) or dict_ip['ip'] == 'banned':
            client_instance.banned = True
            try:
                self.request.close()
                node.logger.app_log.info(f"IP {peer_ip} banned, disconnected")
            except:
                pass
            finally:
                return

        timeout_operation = 120  # timeout
        timer_operation = time.time()  # start counting

        while not client_instance.banned and node.peers.version_allowed(peer_ip, node.version_allow) and client_instance.connected and not node.IS_STOPPING:
            try:
                # Failsafe
                if self.request == -1:
                    raise ValueError(f"Inbound: Closed socket from {peer_ip}")

                if not time.time() <= timer_operation + timeout_operation:  # return on timeout
                    if node.peers.warning(self.request, peer_ip, "Operation timeout", 2):
                        node.logger.app_log.info(f"{peer_ip} banned")
                        break

                    raise ValueError(f"Inbound: Operation timeout from {peer_ip}")

                data = receive(self.request)

                node.logger.app_log.info(
                    f"Inbound: Received: {data} from {peer_ip}")  # will add custom ports later

                if data.startswith('regtest_'):
                    if not node.is_regnet:
                        send(self.request, "notok")
                        return
                    else:
                        db_handler_instance.execute(db_handler_instance.c, "SELECT block_hash FROM transactions WHERE block_height= (select max(block_height) from transactions)")
                        block_hash = db_handler_instance.c.fetchone()[0]
                        # feed regnet with current thread db handle. refactor needed.
                        regnet.conn, regnet.c, regnet.hdd, regnet.h, regnet.hdd2, regnet.h2, regnet.h3 = db_handler_instance.conn, db_handler_instance.c, db_handler_instance.hdd, db_handler_instance.h, db_handler_instance.hdd2, db_handler_instance.h2, db_handler_instance.h3
                        regnet.command(self.request, data, block_hash, node, db_handler_instance)

                if data == 'version':
                    data = receive(self.request)
                    if data not in node.version_allow:
                        node.logger.app_log.warning(
                            f"Protocol version mismatch: {data}, should be {node.version_allow}")
                        send(self.request, "notok")
                        return
                    else:
                        node.logger.app_log.warning(f"Inbound: Protocol version matched with {peer_ip}: {data}")
                        send(self.request, "ok")
                        node.peers.store_mainnet(peer_ip, data)

                elif data == 'getversion':
                    send(self.request, node.version)

                elif data == 'mempool':

                    # receive theirs
                    segments = receive(self.request)
                    node.logger.app_log.info(mp.MEMPOOL.merge(segments, peer_ip, db_handler_instance.c, False))

                    # receive theirs

                    # execute_param(m, ('SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield FROM transactions WHERE timeout < ? ORDER BY amount DESC;'), (int(time.time() - 5),))
                    if mp.MEMPOOL.sendable(peer_ip):
                        # Only send the diff
                        mempool_txs = mp.MEMPOOL.tx_to_send(peer_ip, segments)
                        # and note the time
                        mp.MEMPOOL.sent(peer_ip)
                    else:
                        # We already sent not long ago, send empy
                        mempool_txs = []

                    # send own
                    # node.logger.app_log.info("Inbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: same as the other
                    send(self.request, mempool_txs)

                    # send own

                elif data == "hello":
                    if node.is_regnet:
                        node.logger.app_log.info("Inbound: Got hello but I'm in regtest mode, closing.")
                        return

                    send(self.request, "peers")
                    peers_send = node.peers.peer_list_disk_format()
                    send(self.request, peers_send)

                    while node.db_lock.locked():
                        time.sleep(quantize_two(node.pause_conf))
                    node.logger.app_log.info("Inbound: Sending sync request")

                    send(self.request, "sync")

                elif data == "sendsync":
                    while node.db_lock.locked():
                        time.sleep(quantize_two(node.pause_conf))

                    while len(node.syncing) >= 3:
                        if node.IS_STOPPING:
                            return
                        time.sleep(int(node.pause_conf))

                    send(self.request, "sync")

                elif data == "blocksfnd":
                    node.logger.app_log.info(f"Inbound: Client {peer_ip} has the block(s)")  # node should start sending txs in this step

                    # node.logger.app_log.info("Inbound: Combined segments: " + segments)
                    # print peer_ip
                    if node.db_lock.locked():
                        node.logger.app_log.info(f"Skipping sync from {peer_ip}, syncing already in progress")

                    else:
                        db_handler_instance.execute(db_handler_instance.c,
                                                    "SELECT timestamp FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")  # or it takes the first
                        node.last_block_timestamp = quantize_two(db_handler_instance.c.fetchone()[0])

                        if node.last_block_timestamp < time.time() - 600:
                            # block_req = most_common(consensus_blockheight_list)
                            block_req = node.peers.consensus_most_common
                            node.logger.app_log.warning("Most common block rule triggered")

                        else:
                            # block_req = max(consensus_blockheight_list)
                            block_req = node.peers.consensus_max
                            node.logger.app_log.warning("Longest chain rule triggered")

                        if int(received_block_height) >= block_req:

                            try:  # they claim to have the longest chain, things must go smooth or ban
                                node.logger.app_log.warning(f"Confirming to sync from {peer_ip}")
                                node.plugin_manager.execute_action_hook('sync', {'what': 'syncing_from', 'ip': peer_ip})
                                send(self.request, "blockscf")

                                segments = receive(self.request)

                            except:
                                if node.peers.warning(self.request, peer_ip, "Failed to deliver the longest chain"):
                                    node.logger.app_log.info(f"{peer_ip} banned")
                                    break
                            else:
                                digest_block(node, segments, self.request, peer_ip, db_handler_instance)
                        else:
                            node.logger.app_log.warning(f"Rejecting to sync from {peer_ip}")
                            send(self.request, "blocksrj")
                            node.logger.app_log.info(
                                f"Inbound: Distant peer {peer_ip} is at {received_block_height}, should be at least {block_req}")

                    send(self.request, "sync")

                elif data == "blockheight":
                    try:
                        received_block_height = receive(self.request)  # receive client's last block height
                        node.logger.app_log.info(
                            f"Inbound: Received block height {received_block_height} from {peer_ip} ")

                        # consensus pool 1 (connection from them)
                        consensus_blockheight = int(received_block_height)  # str int to remove leading zeros
                        # consensus_add(peer_ip, consensus_blockheight, self.request)
                        node.peers.consensus_add(peer_ip, consensus_blockheight, self.request, node.last_block)
                        # consensus pool 1 (connection from them)

                        db_handler_instance.execute(db_handler_instance.c, 'SELECT max(block_height) FROM transactions')
                        db_block_height = db_handler_instance.c.fetchone()[0]

                        # append zeroes to get static length
                        send(self.request, db_block_height)
                        # send own block height

                        if int(received_block_height) > db_block_height:
                            node.logger.app_log.warning("Inbound: Client has higher block")

                            db_handler_instance.execute(db_handler_instance.c,
                                                        'SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                            db_block_hash = db_handler_instance.c.fetchone()[0]  # get latest block_hash

                            node.logger.app_log.info(f"Inbound: block_hash to send: {db_block_hash}")
                            send(self.request, db_block_hash)

                            # receive their latest sha_hash
                            # confirm you know that sha_hash or continue receiving

                        elif int(received_block_height) <= db_block_height:
                            if int(received_block_height) == db_block_height:
                                node.logger.app_log.info(
                                    f"Inbound: We have the same height as {peer_ip} ({received_block_height}), hash will be verified")
                            else:
                                node.logger.app_log.warning(
                                    f"Inbound: We have higher ({db_block_height}) block height than {peer_ip} ({received_block_height}), hash will be verified")

                            data = receive(self.request)  # receive client's last block_hash
                            # send all our followup hashes

                            node.logger.app_log.info(f"Inbound: Will seek the following block: {data}")

                            try:
                                db_handler_instance.execute_param(db_handler_instance.h3,
                                                                  "SELECT block_height FROM transactions WHERE block_hash = ?;", (data,))
                                client_block = db_handler_instance.h3.fetchone()[0]
                            except Exception:
                                node.logger.app_log.warning(f"Inbound: Block {data[:8]} of {peer_ip}")
                                send(self.request, "blocknf")
                                send(self.request, data)

                            else:
                                node.logger.app_log.info(f"Inbound: Client is at block {client_block}")  # now check if we have any newer

                                db_handler_instance.execute(db_handler_instance.h3,
                                                            'SELECT block_hash FROM transactions ORDER BY block_height DESC LIMIT 1')
                                db_block_hash = db_handler_instance.h3.fetchone()[0]  # get latest block_hash
                                if db_block_hash == data or not node.egress:
                                    if not node.egress:
                                        node.logger.app_log.warning(f"Outbound: Egress disabled for {peer_ip}")
                                    else:
                                        node.logger.app_log.info(f"Inbound: Client {peer_ip} has the latest block")

                                    time.sleep(int(node.pause_conf))  # reduce CPU usage
                                    send(self.request, "nonewblk")

                                else:

                                    blocks_fetched = []
                                    del blocks_fetched[:]
                                    while sys.getsizeof(
                                            str(blocks_fetched)) < 500000:  # limited size based on txs in blocks
                                        db_handler_instance.execute_param(db_handler_instance.h3, (
                                            "SELECT timestamp,address,recipient,amount,signature,public_key,operation,openfield FROM transactions WHERE block_height > ? AND block_height <= ?;"),
                                                                          (str(int(client_block)), str(int(client_block + 1)),))
                                        result = db_handler_instance.h3.fetchall()
                                        if not result:
                                            break
                                        blocks_fetched.extend([result])
                                        client_block = int(client_block) + 1

                                    # blocks_send = [[l[1:] for l in group] for _, group in groupby(blocks_fetched, key=itemgetter(0))]  # remove block number

                                    # node.logger.app_log.info("Inbound: Selected " + str(blocks_fetched) + " to send")

                                    send(self.request, "blocksfnd")

                                    confirmation = receive(self.request)

                                    if confirmation == "blockscf":
                                        node.logger.app_log.info("Inbound: Client confirmed they want to sync from us")
                                        send(self.request, blocks_fetched)

                                    elif confirmation == "blocksrj":
                                        node.logger.app_log.info(
                                            "Inbound: Client rejected to sync from us because we're don't have the latest block")



                    except Exception as e:
                        node.logger.app_log.info(f"Inbound: Sync failed {e}")

                elif data == "nonewblk":
                    send(self.request, "sync")

                elif data == "blocknf":
                    block_hash_delete = receive(self.request)
                    # print peer_ip
                    if consensus_blockheight == node.peers.consensus_max:
                        blocknf(node, block_hash_delete, peer_ip, db_handler_instance)
                        if node.peers.warning(self.request, peer_ip, "Rollback", 2):
                            node.logger.app_log.info(f"{peer_ip} banned")
                            break
                    node.logger.app_log.info("Outbound: Deletion complete, sending sync request")

                    while node.db_lock.locked():
                        if node.IS_STOPPING:
                            return
                        time.sleep(node.pause_conf)
                    send(self.request, "sync")

                elif data == "block":
                    # if (peer_ip in allowed or "any" in allowed):  # from miner
                    if node.peers.is_allowed(peer_ip, data):  # from miner
                        # TODO: rights management could be done one level higher instead of repeating the same check everywhere

                        node.logger.app_log.info(f"Outbound: Received a block from miner {peer_ip}")
                        # receive block
                        segments = receive(self.request)
                        # node.logger.app_log.info("Inbound: Combined mined segments: " + segments)

                        # check if we have the latest block

                        db_handler_instance.execute(db_handler_instance.c, 'SELECT max(block_height) FROM transactions')
                        db_block_height = int(db_handler_instance.c.fetchone()[0])

                        # check if we have the latest block

                        mined = {"timestamp": time.time(), "last": db_block_height, "ip": peer_ip, "miner": "",
                                 "result": False, "reason": ''}
                        try:
                            mined['miner'] = segments[0][-1][2]
                        except:
                            pass
                        if node.is_mainnet:
                            if len(node.peers.connection_pool) < 5 and not node.peers.is_whitelisted(peer_ip):
                                reason = "Outbound: Mined block ignored, insufficient connections to the network"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.info(reason)
                            elif node.db_lock.locked():
                                reason = "Outbound: Block from miner skipped because we are digesting already"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.warning(reason)
                            elif db_block_height >= node.peers.consensus_max - 3:
                                mined['result'] = True
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.info("Outbound: Processing block from miner")
                                digest_block(node, segments, self.request, peer_ip, db_handler_instance)
                                # This new block may change the int(diff). Trigger the hook whether it changed or not.
                                diff = difficulty(node, db_handler_instance)

                            else:
                                reason = f"Outbound: Mined block was orphaned because node was not synced, we are at block {db_block_height}, should be at least {node.peers.consensus_max - 3}"
                                mined['reason'] = reason
                                node.plugin_manager.execute_action_hook('mined', mined)
                                node.logger.app_log.warning(reason)
                        else:
                            digest_block(node, segments, self.request, peer_ip, db_handler_instance)

                    else:
                        receive(self.request)  # receive block, but do nothing about it
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for block command")

                elif data == "blocklast":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if node.peers.is_allowed(peer_ip, data):
                        db_handler_instance.execute(db_handler_instance.c,
                                                    "SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
                        block_last = db_handler_instance.c.fetchall()[0]
                        logger.app_log.info(f"Status: NODE: blocklast={block_last}")
                        send(self.request, block_last)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blocklast command")

                elif data == "blocklastjson":
                    # if (peer_ip in allowed or "any" in allowed):  # only sends the miner part of the block!
                    if node.peers.is_allowed(peer_ip, data):
                        db_handler_instance.execute(db_handler_instance.c,
                                                    "SELECT * FROM transactions WHERE reward != 0 ORDER BY block_height DESC LIMIT 1;")
                        block_last = db_handler_instance.c.fetchall()[0]

                        response = {"block_height": block_last[0],
                                    "timestamp": block_last[1],
                                    "address": block_last[2],
                                    "recipient": block_last[3],
                                    "amount": block_last[4],
                                    "signature": block_last[5],
                                    "public_key": block_last[6],
                                    "block_hash": block_last[7],
                                    "fee": block_last[8],
                                    "reward": block_last[9],
                                    "operation": block_last[10],
                                    "nonce": block_last[11]}

                        send(self.request, response)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blocklastjson command")

                elif data == "blockget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        block_desired = receive(self.request)

                        db_handler_instance.execute_param(db_handler_instance.h3, "SELECT * FROM transactions WHERE block_height = ?;",
                                                          (block_desired,))
                        block_desired_result = db_handler_instance.h3.fetchall()

                        send(self.request, block_desired_result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blockget command")

                elif data == "blockgetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        block_desired = receive(self.request)

                        db_handler_instance.execute_param(db_handler_instance.h3, "SELECT * FROM transactions WHERE block_height = ?;",
                                                          (block_desired,))
                        block_desired_result = db_handler_instance.h3.fetchall()

                        response_list = []
                        for transaction in block_desired_result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        send(self.request, response_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for blockget command")

                elif data == "mpinsert":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        mempool_insert = receive(self.request)
                        node.logger.app_log.warning("mpinsert command")

                        mpinsert_result = mp.MEMPOOL.merge(mempool_insert, peer_ip, db_handler_instance.c, True, True)
                        node.logger.app_log.warning(f"mpinsert result: {mpinsert_result}")
                        send(self.request, mpinsert_result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for mpinsert command")

                elif data == "balanceget":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)

                        send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info("{peer_ip} not whitelisted for balanceget command")

                elif data == "balancegetjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)
                        response = {"balance": balanceget_result[0],
                                    "credit": balanceget_result[1],
                                    "debit": balanceget_result[2],
                                    "fees": balanceget_result[3],
                                    "rewards": balanceget_result[4],
                                    "balance_no_mempool": balanceget_result[5]}

                        send(self.request,
                                         response)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for balancegetjson command")

                elif data == "balancegethyper":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)[0]

                        send(self.request,
                                         balanceget_result)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for balancegetjson command")

                elif data == "balancegethyperjson":
                    if node.peers.is_allowed(peer_ip, data):
                        balance_address = receive(self.request)  # for which address

                        balanceget_result = balanceget(balance_address, db_handler_instance)
                        response = {"balance": balanceget_result[0]}

                        send(self.request,
                                         response)  # return balance of the address to the client, including mempool
                        # send(self.request, balance_pre)  # return balance of the address to the client, no mempool
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for balancegethyperjson command")

                elif data == "mpgetjson" and node.peers.is_allowed(peer_ip, data):
                    mempool_txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)

                    response_list = []
                    for transaction in mempool_txs:
                        response = {"timestamp": transaction[0],
                                    "address": transaction[1],
                                    "recipient": transaction[2],
                                    "amount": transaction[3],
                                    "signature": transaction[4],
                                    "public_key": transaction[5],
                                    "operation": transaction[6],
                                    "openfield": transaction[7]}

                        response_list.append(response)

                    # node.logger.app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    send(self.request, response_list)

                elif data == "mpget" and node.peers.is_allowed(peer_ip, data):
                    mempool_txs = mp.MEMPOOL.fetchall(mp.SQL_SELECT_TX_TO_SEND)

                    # node.logger.app_log.info("Outbound: Extracted from the mempool: " + str(mempool_txs))  # improve: sync based on signatures only

                    # if len(mempool_txs) > 0: #wont sync mempool until we send something, which is bad
                    # send own
                    send(self.request, mempool_txs)

                elif data == "mpclear" and peer_ip == "127.0.0.1":  # reserved for localhost
                    mp.MEMPOOL.clear()

                elif data == "keygen":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = keys.generate()
                        send(self.request, (gen_private_key_readable, gen_public_key_readable, gen_address))
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for keygen command")

                elif data == "keygenjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = keys.generate()
                        response = {"private_key": gen_private_key_readable,
                                    "public_key": gen_public_key_readable,
                                    "address": gen_address}

                        send(self.request, response)
                        (gen_private_key_readable, gen_public_key_readable, gen_address) = (None, None, None)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for keygen command")

                elif data == "addlist":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        db_handler_instance.execute_param(db_handler_instance.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC"),
                                                          (address_tx_list, address_tx_list,))
                        result = db_handler_instance.h3.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlist command")

                elif data == "listlimjson":
                    # if (peer_ip in allowed or "any" in allowed):
                    if node.peers.is_allowed(peer_ip, data):
                        list_limit = receive(self.request)
                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h3, "SELECT * FROM transactions ORDER BY block_height DESC LIMIT ?",
                                                          (list_limit,))
                        result = db_handler_instance.h3.fetchall()

                        response_list = []
                        for transaction in result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        send(self.request, response_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for listlimjson command")

                elif data == "listlim":
                    if node.peers.is_allowed(peer_ip, data):
                        list_limit = receive(self.request)
                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h3, "SELECT * FROM transactions ORDER BY block_height DESC LIMIT ?",
                                                          (list_limit,))
                        result = db_handler_instance.h3.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for listlim command")

                elif data == "addlistlim":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h3.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlim command")

                elif data == "addlistlimjson":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) ORDER BY block_height DESC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h3.fetchall()

                        response_list = []
                        for transaction in result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        send(self.request, response_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimjson command")

                elif data == "addlistlimmir":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) AND block_height < 1 ORDER BY block_height ASC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h3.fetchall()
                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimmir command")

                elif data == "addlistlimmirjson":
                    if node.peers.is_allowed(peer_ip, data):
                        address_tx_list = receive(self.request)
                        address_tx_list_limit = receive(self.request)

                        # print(address_tx_list_limit)
                        db_handler_instance.execute_param(db_handler_instance.h3, (
                            "SELECT * FROM transactions WHERE (address = ? OR recipient = ?) AND block_height < 1 ORDER BY block_height ASC LIMIT ?"),
                                                          (address_tx_list, address_tx_list, address_tx_list_limit,))
                        result = db_handler_instance.h3.fetchall()

                        response_list = []
                        for transaction in result:
                            response = {"block_height": transaction[0],
                                        "timestamp": transaction[1],
                                        "address": transaction[2],
                                        "recipient": transaction[3],
                                        "amount": transaction[4],
                                        "signature": transaction[5],
                                        "public_key": transaction[6],
                                        "block_hash": transaction[7],
                                        "fee": transaction[8],
                                        "reward": transaction[9],
                                        "operation": transaction[10],
                                        "openfield": transaction[11]}

                            response_list.append(response)

                        send(self.request, response_list)

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addlistlimmir command")


                elif data == "aliasget":  # all for a single address, no protection against overlapping
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", node.logger.app_log)

                        alias_address = receive(self.request)

                        db_handler_instance.execute_param(db_handler_instance.index_cursor, "SELECT alias FROM aliases WHERE address = ? ",
                                                          (alias_address,))

                        result = db_handler_instance.index_cursor.fetchall()

                        if not result:
                            result = [[alias_address]]

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasget command")

                elif data == "aliasesget":  # only gets the first one, for multiple addresses
                    if node.peers.is_allowed(peer_ip, data):
                        aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", node.logger.app_log)

                        aliases_request = receive(self.request)

                        results = []
                        for alias_address in aliases_request:
                            db_handler_instance.execute_param(db_handler_instance.index_cursor, (
                                "SELECT alias FROM aliases WHERE address = ? ORDER BY block_height ASC LIMIT 1"),
                                                              (alias_address,))
                            try:
                                result = db_handler_instance.index_cursor.fetchall()[0][0]
                            except:
                                result = alias_address
                            results.append(result)

                        send(self.request, results)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliasesget command")

                # Not mandatory, but may help to reindex with minimal sql queries
                elif data == "tokensupdate":
                    if node.peers.is_allowed(peer_ip, data):
                        tokens.tokens_update(node.index_db, node.ledger_path_conf, "normal", node.logger.app_log,
                                             node.plugin_manager)
                #
                elif data == "tokensget":
                    if node.peers.is_allowed(peer_ip, data):
                        tokens.tokens_update(node.index_db, node.ledger_path_conf, "normal", node.logger.app_log,
                                             node.plugin_manager)
                        tokens_address = receive(self.request)

                        db_handler_instance.index_cursor.execute(
                            "SELECT DISTINCT token FROM tokens WHERE address OR recipient = ?", (tokens_address,))
                        tokens_user = db_handler_instance.index_cursor.fetchall()

                        tokens_list = []
                        for token in tokens_user:
                            token = token[0]
                            db_handler_instance.execute_param(db_handler_instance.index_cursor,
                                                              "SELECT sum(amount) FROM tokens WHERE recipient = ? AND token = ?;",
                                                              (tokens_address,) + (token,))
                            credit = db_handler_instance.index_cursor.fetchone()[0]
                            db_handler_instance.execute_param(db_handler_instance.index_cursor,
                                                              "SELECT sum(amount) FROM tokens WHERE address = ? AND token = ?;",
                                                              (tokens_address,) + (token,))
                            debit = db_handler_instance.index_cursor.fetchone()[0]

                            debit = 0 if debit is None else debit
                            credit = 0 if credit is None else credit

                            balance = str(Decimal(credit) - Decimal(debit))

                            tokens_list.append((token, balance))

                        send(self.request, tokens_list)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for tokensget command")

                elif data == "addfromalias":
                    if node.peers.is_allowed(peer_ip, data):

                        aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", node.logger.app_log)

                        alias_address = receive(self.request)
                        db_handler_instance.execute_param(db_handler_instance.index_cursor,
                                                          "SELECT address FROM aliases WHERE alias = ? ORDER BY block_height ASC LIMIT 1;",
                                                          (alias_address,))  # asc for first entry
                        try:
                            address_fetch = db_handler_instance.index_cursor.fetchone()[0]
                        except:
                            address_fetch = "No alias"
                        node.logger.app_log.warning(f"Fetched the following alias address: {address_fetch}")

                        send(self.request, address_fetch)

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addfromalias command")

                elif data == "pubkeyget":
                    if node.peers.is_allowed(peer_ip, data):
                        pub_key_address = receive(self.request)

                        db_handler_instance.c.execute_param(
                            "SELECT public_key FROM transactions WHERE address = ? and reward = 0 LIMIT 1",
                            pub_key_address, )
                        target_public_key_hashed = db_handler_instance.c.fetchone()[0]
                        send(self.request, target_public_key_hashed)

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for pubkeyget command")

                elif data == "aliascheck":
                    if node.peers.is_allowed(peer_ip, data):
                        reg_string = receive(self.request)

                        registered_pending = mp.MEMPOOL.fetchone(
                            "SELECT timestamp FROM transactions WHERE openfield = ?;",
                            ("alias=" + reg_string,))

                        db_handler_instance.execute_param(db_handler_instance.h3, "SELECT timestamp FROM transactions WHERE openfield = ?;", ("alias=" + reg_string,) )
                        registered_already = db_handler_instance.h3.fetchone()

                        if registered_already is None and registered_pending is None:
                            send(self.request, "Alias free")
                        else:
                            send(self.request, "Alias registered")
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for aliascheck command")

                elif data == "txsend":
                    if node.peers.is_allowed(peer_ip, data):
                        tx_remote = receive(self.request)

                        # receive data necessary for remote tx construction
                        remote_tx_timestamp = tx_remote[0]
                        remote_tx_privkey = tx_remote[1]
                        remote_tx_recipient = tx_remote[2]
                        remote_tx_amount = tx_remote[3]
                        remote_tx_operation = tx_remote[4]
                        remote_tx_openfield = tx_remote[5]
                        # receive data necessary for remote tx construction

                        # derive remaining data
                        tx_remote_key = RSA.importKey(remote_tx_privkey)
                        remote_tx_pubkey = tx_remote_key.publickey().exportKey().decode("utf-8")

                        remote_tx_pubkey_hashed = base64.b64encode(remote_tx_pubkey.encode('utf-8')).decode("utf-8")

                        remote_tx_address = hashlib.sha224(remote_tx_pubkey.encode("utf-8")).hexdigest()
                        # derive remaining data

                        # construct tx
                        remote_tx = (str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient),
                                     '%.8f' % quantize_eight(remote_tx_amount), str(remote_tx_operation),
                                     str(remote_tx_openfield))  # this is signed

                        remote_hash = SHA.new(str(remote_tx).encode("utf-8"))
                        remote_signer = PKCS1_v1_5.new(tx_remote_key)
                        remote_signature = remote_signer.sign(remote_hash)
                        remote_signature_enc = base64.b64encode(remote_signature).decode("utf-8")
                        # construct tx

                        # insert to mempool, where everything will be verified
                        mempool_data = ((str(remote_tx_timestamp), str(remote_tx_address), str(remote_tx_recipient),
                                         '%.8f' % quantize_eight(remote_tx_amount), str(remote_signature_enc),
                                         str(remote_tx_pubkey_hashed), str(remote_tx_operation),
                                         str(remote_tx_openfield)))

                        node.logger.app_log.info(mp.MEMPOOL.merge(mempool_data, peer_ip, db_handler_instance.c, True, True))

                        send(self.request, str(remote_signature_enc))
                        # wipe variables
                        (tx_remote, remote_tx_privkey, tx_remote_key) = (None, None, None)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for txsend command")

                # less important methods
                elif data == "addvalidate":
                    if node.peers.is_allowed(peer_ip, data):

                        address_to_validate = receive(self.request)
                        if essentials.address_validate(address_to_validate):
                            result = "valid"
                        else:
                            result = "invalid"

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for addvalidate command")

                elif data == "annget":
                    if node.peers.is_allowed(peer_ip):

                        # with open(peerlist, "r") as peer_list:
                        #    peers_file = peer_list.read()

                        try:
                            db_handler_instance.execute_param(db_handler_instance.h3, "SELECT openfield FROM transactions WHERE address = ? AND operation = ? ORDER BY block_height DESC LIMIT 1", (node.genesis_conf, "ann"))
                            result = db_handler_instance.h3.fetchone()[0]
                        except:
                            result = ""

                        send(self.request, result)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for annget command")

                elif data == "annverget":
                    if node.peers.is_allowed(peer_ip):

                        try:
                            db_handler_instance.execute_param(db_handler_instance.h3, "SELECT openfield FROM transactions WHERE address = ? AND operation = ? ORDER BY block_height DESC LIMIT 1", (node.genesis_conf, "annver"))
                            result = db_handler_instance.h3.fetchone()[0]
                        except:
                            result = ""

                        send(self.request, result)

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for annget command")

                elif data == "peersget":
                    if node.peers.is_allowed(peer_ip, data):
                        send(self.request, node.peers.peer_list_disk_format())

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for peersget command")

                elif data == "statusget":
                    if node.peers.is_allowed(peer_ip, data):

                        nodes_count = node.peers.consensus_size
                        nodes_list = node.peers.peer_opinion_dict
                        threads_count = threading.active_count()
                        uptime = int(time.time() - node.startup_time)
                        diff = node.difficulty
                        server_timestamp = '%.2f' % time.time()

                        if node.reveal_address:
                            revealed_address = node_keys.address

                        else:
                            revealed_address = "private"

                        send(self.request, (
                            revealed_address, nodes_count, nodes_list, threads_count, uptime, node.peers.consensus,
                            node.peers.consensus_percentage, app_version, diff, server_timestamp))

                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for statusget command")

                elif data == "statusjson":
                    if node.peers.is_allowed(peer_ip, data):
                        uptime = int(time.time() - node.startup_time)
                        tempdiff = node.difficulty

                        if node.reveal_address:
                            revealed_address = node_keys.address
                        else:
                            revealed_address = "private"

                        status = {"protocolversion": node.version,
                                  "address": revealed_address,
                                  "walletversion": app_version,
                                  "testnet": node.is_testnet,  # config data
                                  "blocks": node.last_block, "timeoffset": 0,
                                  "connections": node.peers.consensus_size,
                                  "connections_list": node.peers.peer_opinion_dict,
                                  "difficulty": tempdiff[0],  # live status, bitcoind format
                                  "threads": threading.active_count(),
                                  "uptime": uptime, "consensus": node.peers.consensus,
                                  "consensus_percent": node.peers.consensus_percentage,
                                  "server_timestamp": '%.2f' % time.time()}  # extra data
                        if node.is_regnet:
                            status['regnet'] = True
                        send(self.request, status)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for statusjson command")
                elif data[:4] == 'api_':
                    if node.peers.is_allowed(peer_ip, data):
                        try:
                            node.apihandler.dispatch(data, self.request, db_handler_instance, node.peers)
                        except Exception as e:
                            print(e)

                elif data == "diffget":
                    if node.peers.is_allowed(peer_ip, data):
                        diff = node.difficulty
                        send(self.request, diff)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for diffget command")

                elif data == "diffgetjson":
                    if node.peers.is_allowed(peer_ip, data):
                        diff = node.difficulty
                        response = {"difficulty": diff[0],
                                    "diff_dropped": diff[0],
                                    "time_to_generate": diff[0],
                                    "diff_block_previous": diff[0],
                                    "block_time": diff[0],
                                    "hashrate": diff[0],
                                    "diff_adjustment": diff[0],
                                    "block_height": diff[0]}

                        send(self.request, response)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for diffgetjson command")

                elif data == "difflast":
                    if node.peers.is_allowed(peer_ip, data):

                        db_handler_instance.execute(db_handler_instance.h3,
                                                    "SELECT block_height, difficulty FROM misc ORDER BY block_height DESC LIMIT 1")
                        difflast = db_handler_instance.h3.fetchone()
                        send(self.request, difflast)
                    else:
                        node.logger.app_log.info("f{peer_ip} not whitelisted for difflastget command")

                elif data == "difflastjson":
                    if node.peers.is_allowed(peer_ip, data):

                        db_handler_instance.execute(db_handler_instance.h3,
                                                    "SELECT block_height, difficulty FROM misc ORDER BY block_height DESC LIMIT 1")
                        difflast = db_handler_instance.h3.fetchone()
                        response = {"block": difflast[0],
                                    "difficulty": difflast[1]
                                    }
                        send(self.request, response)
                    else:
                        node.logger.app_log.info(f"{peer_ip} not whitelisted for difflastjson command")

                elif data == "stop":
                    if node.peers.is_allowed(peer_ip, data):
                        node.logger.app_log.warning(f"Received stop from {peer_ip}")
                        node.IS_STOPPING = True


                elif data == "hyperlane":
                    pass

                else:
                    if data == '*':
                        raise ValueError("Broken pipe")
                    raise ValueError("Unexpected error, received: " + str(data)[:32] + ' ...')

                if not time.time() <= timer_operation + timeout_operation:
                    timer_operation = time.time()  # reset timer
                # time.sleep(float(node.pause_conf))  # prevent cpu overload
                node.logger.app_log.info(f"Server loop finished for {peer_ip}")


            except Exception as e:
                node.logger.app_log.info(f"Inbound: Lost connection to {peer_ip}")
                node.logger.app_log.info(f"Inbound: {e}")

                # remove from consensus (connection from them)
                node.peers.consensus_remove(peer_ip)
                # remove from consensus (connection from them)
                if self.request:
                    self.request.close()

                if node.debug_conf:
                    raise  # major debug client
                else:
                    return

        if not node.peers.version_allowed(peer_ip, node.version_allow):
            node.logger.app_log.warning(f"Inbound: Closing connection to old {peer_ip} node: {node.peers.ip_to_mainnet['peer_ip']}")


def ensure_good_peer_version(peer_ip):
    """
    cleanup after HF, kepts here for future use.
    """
    """
    # If we are post fork, but we don't know the version, then it was an old connection, close.
    if is_mainnet and (node.last_block >= POW_FORK) :
        if peer_ip not in node.peers.ip_to_mainnet:
            raise ValueError("Outbound: disconnecting old node {}".format(peer_ip));
        elif node.peers.ip_to_mainnet[peer_ip] not in node.version_allow:
            raise ValueError("Outbound: disconnecting old node {} - {}".format(peer_ip, node.peers.ip_to_mainnet[peer_ip]));
    """


# client thread
# if you "return" from the function, the exception code will node be executed and client thread will hang


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


def just_int_from(s):
    return int(''.join(i for i in s if i.isdigit()))


def setup_net_type():
    """
    Adjust globals depending on mainnet, testnet or regnet
    """
    # Defaults value, dup'd here for clarity sake.
    node.is_mainnet = True
    node.is_testnet = False
    node.is_regnet = False

    if "testnet" in node.version or node.is_testnet:
        node.is_testnet = True
        node.is_mainnet = False

    if "regnet" in node.version or node.is_regnet:
        node.is_regnet = True
        node.is_testnet = False
        node.is_mainnet = False

    node.logger.app_log.warning(f"Testnet: {node.is_testnet}")
    node.logger.app_log.warning(f"Regnet : {node.is_regnet}")

    # default mainnet config
    node.peerfile = "peers.txt"
    node.ledger_ram_file = "file:ledger?mode=memory&cache=shared"
    node.index_db = "static/index.db"

    if node.is_mainnet:
        # Allow 18 for transition period. Will be auto removed at fork block.
        if node.version != 'mainnet0020':
            node.version = 'mainnet0019'  # Force in code.
        if "mainnet0020" not in node.version_allow:
            node.version_allow = ['mainnet0019', 'mainnet0020', 'mainnet0021']
        # Do not allow bad configs.
        if not 'mainnet' in node.version:
            node.logger.app_log.error("Bad mainnet version, check config.txt")
            sys.exit()
        num_ver = just_int_from(node.version)
        if num_ver < 19:
            node.logger.app_log.error("Too low mainnet version, check config.txt")
            sys.exit()
        for allowed in node.version_allow:
            num_ver = just_int_from(allowed)
            if num_ver < 19:
                node.logger.app_log.error("Too low allowed version, check config.txt")
                sys.exit()

    if node.is_testnet:
        node.port = 2829
        node.full_ledger = False
        node.hyper_path_conf = "static/test.db"
        node.ledger_path_conf = "static/test.db"  # for tokens
        node.ledger_ram_file = "file:ledger_testnet?mode=memory&cache=shared"
        node.hyper_recompress_conf = False
        node.peerfile = "peers_test.txt"
        node.index_db = "static/index_test.db"
        if not 'testnet' in node.version:
            node.logger.app_log.error("Bad testnet version, check config.txt")
            sys.exit()

        redownload_test = input("Status: Welcome to the testnet. Redownload test ledger? y/n")
        if redownload_test == "y" or not os.path.exists("static/test.db"):
            types = ['static/test.db-wal', 'static/test.db-shm', 'static/index_test.db']
            for type in types:
                for file in glob.glob(type):
                    os.remove(file)
                    print(file, "deleted")
            download_file("https://bismuth.cz/test.db", "static/test.db")
            download_file("https://bismuth.cz/index_test.db", "static/index_test.db")
        else:
            print("Not redownloading test db")

    if node.is_regnet:
        node.port = regnet.REGNET_PORT
        node.full_ledger = False
        node.hyper_path_conf = regnet.REGNET_DB
        node.ledger_path_conf = regnet.REGNET_DB
        node.ledger_ram_file = "file:ledger_regnet?mode=memory&cache=shared"
        node.hyper_recompress_conf = False
        node.peerfile = regnet.REGNET_PEERS
        node.index_db = regnet.REGNET_INDEX
        if not 'regnet' in node.version:
            node.logger.app_log.error("Bad regnet version, check config.txt")
            sys.exit()
        node.logger.app_log.warning("Regnet init...")
        regnet.init(node.logger.app_log)
        regnet.DIGEST_BLOCK = digest_block
        mining_heavy3.is_regnet = True
        """
        node.logger.app_log.warning("Regnet still is WIP atm.")
        sys.exit()
        """


def initial_db_check(database):
    """
    Initial bootstrap check and chain validity control
    """
    # force bootstrap via adding an empty "fresh_sync" file in the dir.
    if os.path.exists("fresh_sync") and node.is_mainnet:
        node.logger.app_log.warning("Status: Fresh sync required, bootstrapping from the website")
        os.remove("fresh_sync")
        bootstrap()
    # UPDATE mainnet DB if required
    if node.is_mainnet:
        upgrade = sqlite3.connect(node.ledger_path_conf)
        u = upgrade.cursor()
        try:
            u.execute("PRAGMA table_info(transactions);")
            result = u.fetchall()[10][2]
            if result != "TEXT":
                raise ValueError("Database column type outdated for Command field")
            upgrade.close()
        except Exception as e:
            print(e)
            upgrade.close()
            print("Database needs upgrading, bootstrapping...")
            bootstrap()

    node.logger.app_log.warning(f"Status: Indexing tokens from ledger {node.ledger_path_conf}")
    tokens.tokens_update(node.index_db, node.ledger_path_conf, "normal", node.logger.app_log, node.plugin_manager)
    node.logger.app_log.warning("Status: Indexing aliases")
    aliases.aliases_update(node.index_db, node.ledger_path_conf, "normal", node.logger.app_log)

    try:
        source_db = sqlite3.connect(node.hyper_path_conf, timeout=1)
        source_db.text_factory = str
        sc = source_db.cursor()

        sc.execute("SELECT max(block_height) FROM transactions")
        node.hdd_block = sc.fetchone()[0]

        node.last_block = node.hdd_block
        checkpoint_set(node, node.hdd_block)

        if node.is_mainnet and (node.hdd_block >= POW_FORK - FORK_AHEAD):
            limit_version(node)

        if node.ram_conf:
            node.logger.app_log.warning("Status: Moving database to RAM")
            database.to_ram = sqlite3.connect(node.ledger_ram_file, uri=True, timeout=1, isolation_level=None)
            database.to_ram.text_factory = str
            database.tr = database.to_ram.cursor()

            query = "".join(line for line in source_db.iterdump())
            database.to_ram.executescript(query)
            # do not close
            node.logger.app_log.warning("Status: Moved database to RAM")

    except Exception as e:
        node.logger.app_log.error(e)
        sys.exit()


def load_keys():
    """Initial loading of crypto keys"""

    essentials.keys_check(node.logger.app_log, "wallet.der")

    node_keys.key, node_keys.public_key_readable, node_keys.private_key_readable, _, _, node_keys.public_key_hashed, node_keys.address, node_keys.keyfile = essentials.keys_load(
        "privkey.der", "pubkey.der")

    if node.is_regnet:
        regnet.PRIVATE_KEY_READABLE = node_keys.private_key_readable
        regnet.PUBLIC_KEY_HASHED = node_keys.public_key_hashed
        regnet.ADDRESS = node_keys.address
        regnet.KEY = node_keys.key

    node.logger.app_log.warning(f"Status: Local address: {node_keys.address}")


def verify(db_handler):
    try:
        node.logger.app_log.warning("Blockchain verification started...")
        # verify blockchain
        db_handler.execute(db_handler.h3, "SELECT Count(*) FROM transactions")
        db_rows = db_handler.h3.fetchone()[0]
        node.logger.app_log.warning("Total steps: {}".format(db_rows))

        # verify genesis
        if node.full_ledger:
            db_handler.execute(db_handler.h3, "SELECT block_height, recipient FROM transactions WHERE block_height = 1")
            result = db_handler.h3.fetchall()[0]
            block_height = result[0]
            genesis = result[1]
            node.logger.app_log.warning(f"Genesis: {genesis}")
            if str(genesis) != node.genesis_conf and int(
                    block_height) == 0:
                node.logger.app_log.warning("Invalid genesis address")
                sys.exit(1)
        # verify genesis

        db_hashes = {
            '27258-1493755375.23': 'acd6044591c5baf121e581225724fc13400941c7',
            '27298-1493755830.58': '481ec856b50a5ae4f5b96de60a8eda75eccd2163',
            '30440-1493768123.08': 'ed11b24530dbcc866ce9be773bfad14967a0e3eb',
            '32127-1493775151.92': 'e594d04ad9e554bce63593b81f9444056dd1705d',
            '32128-1493775170.17': '07a8c49d00e703f1e9518c7d6fa11d918d5a9036',
            '37732-1493799037.60': '43c064309eff3b3f065414d7752f23e1de1e70cd',
            '37898-1493799317.40': '2e85b5c4513f5e8f3c83a480aea02d9787496b7a',
            '37898-1493799774.46': '4ea899b3bdd943a9f164265d51b9427f1316ce39',
            '38083-1493800650.67': '65e93aab149c7e77e383e0f9eb1e7f9a021732a0',
            '52233-1493876901.73': '29653fdefc6ca98aadeab37884383fedf9e031b3',
            '52239-1493876963.71': '4c0e262de64a5e792601937a333ca2bf6d6681f2',
            '52282-1493877169.29': '808f90534e7ba68ee60bb2ea4530f5ff7b9d8dea',
            '52308-1493877257.85': '8919548fdbc5093a6e9320818a0ca058449e29c2',
            '52393-1493877463.97': '0eba7623a44441d2535eafea4655e8ef524f3719',
            '62507-1493946372.50': '81c9ca175d09f47497a57efeb51d16ee78ddc232',
            '70094-1494032933.14': '2ca4403387e84b95ed558e7c9350c43efff8225c'
        }
        invalid = 0

        for row in db_handler.h3.execute('SELECT * FROM transactions WHERE block_height > 1 and reward = 0 ORDER BY block_height'):  # native sql fx to keep compatibility

            db_block_height = str(row[0])
            db_timestamp = '%.2f' % (quantize_two(row[1]))
            db_address = str(row[2])[:56]
            db_recipient = str(row[3])[:56]
            db_amount = '%.8f' % (quantize_eight(row[4]))
            db_signature_enc = str(row[5])[:684]
            db_public_key_hashed = str(row[6])[:1068]
            db_public_key = RSA.importKey(base64.b64decode(db_public_key_hashed))
            db_operation = str(row[10])[:30]
            db_openfield = str(row[11])  # no limit for backward compatibility

            db_transaction = (db_timestamp, db_address, db_recipient, db_amount, db_operation, db_openfield)

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            sha_hash = SHA.new(str(db_transaction).encode("utf-8"))
            if verifier.verify(sha_hash, db_signature_dec):
                pass
            else:
                try:
                    if sha_hash.hexdigest() != db_hashes[db_block_height + "-" + db_timestamp]:
                        node.logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                        invalid = invalid + 1
                except:
                    node.logger.app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                    invalid = invalid + 1

        if invalid == 0:
            node.logger.app_log.warning("All transacitons in the local ledger are valid")

    except Exception as e:
        node.logger.app_log.warning("Error: {}".format(e))
        raise


if __name__ == "__main__":
    # classes
    q = queue.Queue()
    node = classes.Node()
    node.logger = classes.Logger()
    node_keys = classes.Keys()

    node.is_testnet = False
    # regnet takes over testnet
    node.is_regnet = False
    # if it's not testnet, nor regnet, it's mainnet
    node.is_mainnet = True

    config = options.Get()
    config.read()
    # classes



    node.app_version = app_version
    node.version = config.version_conf
    node.debug_level = config.debug_level_conf
    node.port = config.port
    node.verify_conf = config.verify_conf
    node.thread_limit_conf = config.thread_limit_conf
    node.rebuild_db_conf = config.rebuild_db_conf
    node.debug_conf = config.debug_conf
    node.debug_level_conf = config.debug_level_conf
    node.pause_conf = config.pause_conf
    node.ledger_path_conf = config.ledger_path_conf
    node.hyper_path_conf = config.hyper_path_conf
    node.hyper_recompress_conf = config.hyper_recompress_conf
    node.tor_conf = config.tor_conf
    node.ram_conf = config.ram_conf
    node.version_allow = config.version_allow
    node.full_ledger = config.full_ledger_conf
    node.reveal_address = config.reveal_address
    node.terminal_output = config.terminal_output
    node.egress = config.egress
    node.genesis_conf = config.genesis_conf
    node.accept_peers = config.accept_peers

    node.IS_STOPPING = False

    node.logger.app_log = log.log("node.log", node.debug_level_conf, node.terminal_output)
    node.logger.app_log.warning("Configuration settings loaded")

    # upgrade wallet location after nuitka-required "files" folder introduction
    if os.path.exists("../wallet.der") and not os.path.exists("wallet.der") and "Windows" in platform.system():
        print("Upgrading wallet location")
        os.rename("../wallet.der", "wallet.der")
    # upgrade wallet location after nuitka-required "files" folder introduction

    mining_heavy3.mining_open()
    try:
        # create a plugin manager, load all plugin modules and init
        node.plugin_manager = plugins.PluginManager(app_log=node.logger.app_log, init=True)

        setup_net_type()
        load_keys()

        node.logger.app_log.warning(f"Status: Starting node version {app_version}")
        node.startup_time = time.time()
        try:

            node.peers = peershandler.Peers(node.logger.app_log, config, node)

            # print(peers.peer_list_old_format())
            # sys.exit()

            node.apihandler = apihandler.ApiHandler(node.logger.app_log, config)
            mp.MEMPOOL = mp.Mempool(node.logger.app_log, config, node.db_lock, node.is_testnet)

            check_integrity(node.hyper_path_conf)
            #PLACEHOLDER FOR FRESH HYPERBLOCK BUILDER

            # if node.rebuild_db_conf: #does nothing
            #    db_maintenance(init_database)

            # db_manager = db_looper.DbManager(node.logger.app_log)
            # db_manager.start()

            db_handler_initial = dbhandler.DbHandler(node.index_db, node.ledger_path_conf, node.hyper_path_conf, node.full_ledger, node.ram_conf, node.ledger_ram_file, node.logger)
            ledger_check_heights(node, db_handler_initial)
            


            initial_db_check(db_handler_initial)
            if not node.is_regnet:
                sequencing_check(db_handler_initial)

            if node.verify_conf:
                verify(db_handler_initial)

            db_handler_initial.close_all()

            if not node.tor_conf:
                # Port 0 means to select an arbitrary unused port
                host, port = "0.0.0.0", int(node.port)

                ThreadedTCPServer.allow_reuse_address = True
                ThreadedTCPServer.daemon_threads = True
                ThreadedTCPServer.timeout = 60
                ThreadedTCPServer.request_queue_size = 100

                server = ThreadedTCPServer((host, port), ThreadedTCPRequestHandler)
                ip, node.port = server.server_address

                # Start a thread with the server -- that thread will then start one
                # more thread for each request

                server_thread = threading.Thread(target=server.serve_forever)
                server_thread.daemon = True
                server_thread.start()

                node.logger.app_log.warning("Status: Server loop running.")

            else:
                node.logger.app_log.warning("Status: Not starting a local server to conceal identity on Tor network")

            # hyperlane_manager = hyperlane.HyperlaneManager(node.logger.app_log)
            # hyperlane_manager.start()

            # start connection manager
            connection_manager = connectionmanager.ConnectionManager(node, mp)
            connection_manager.start()
            # start connection manager


        except Exception as e:
            node.logger.app_log.info(e)
            raise

    except Exception as e:
        node.logger.app_log.info(e)
        raise

    node.logger.app_log.warning("Status: Bismuth loop running.")


    while True:
        if node.IS_STOPPING:
            if node.db_lock.locked():
                time.sleep(0.1)
            else:
                mining_heavy3.mining_close()
                node.logger.app_log.warning("Status: Successfully stopped.")
        time.sleep(1)
