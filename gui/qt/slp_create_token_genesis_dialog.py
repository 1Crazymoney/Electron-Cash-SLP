import copy
import datetime
from functools import partial
import json
import threading
import sys, traceback

from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PyQt5.QtWidgets import *

from electroncash.address import Address, PublicKey
from electroncash.bitcoin import base_encode, TYPE_ADDRESS
from electroncash.i18n import _
from electroncash.plugins import run_hook

from .util import *

from electroncash.util import bfh, format_satoshis_nofloat, format_satoshis_plain_nofloat, NotEnoughFunds, ExcessiveFee
from electroncash.transaction import Transaction
from electroncash.slp import SlpMessage, SlpUnsupportedSlpTokenType, SlpInvalidOutputMessage, buildGenesisOpReturnOutput_V1

from .amountedit import SLPAmountEdit
from .transaction_dialog import show_transaction

from .bfp_upload_file_dialog import BitcoinFilesUploadDialog

from electroncash import networks

dialogs = []  # Otherwise python randomly garbage collects the dialogs...

class SlpCreateTokenGenesisDialog(QDialog, MessageBoxMixin):

    def __init__(self, main_window):
        #self.provided_token_name = token_name
        # We want to be a top-level window
        QDialog.__init__(self, parent=main_window)

        self.main_window = main_window
        self.wallet = main_window.wallet
        self.config = main_window.config
        self.network = main_window.network
        self.app = main_window.app


        self.setWindowTitle(_("Create a New Token"))

        vbox = QVBoxLayout()
        self.setLayout(vbox)

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        vbox.addLayout(grid)
        row = 0

        msg = _('An optional name string embedded in the token genesis transaction.')
        grid.addWidget(HelpLabel(_('Token Name (optional):'), msg), row, 0)
        self.token_name_e = QLineEdit()
        grid.addWidget(self.token_name_e, row, 1)
        row += 1

        msg = _('An optional ticker symbol string embedded into the token genesis transaction.')
        grid.addWidget(HelpLabel(_('Ticker Symbol (optional):'), msg), row, 0)
        self.token_ticker_e = QLineEdit()
        self.token_ticker_e.setFixedWidth(110)
        self.token_ticker_e.textChanged.connect(self.upd_token)
        grid.addWidget(self.token_ticker_e, row, 1)
        row += 1

        msg = _('An optional URL string embedded into the token genesis transaction.')
        grid.addWidget(HelpLabel(_('Document URL (optional):'), msg), row, 0)
        self.token_url_e = QLineEdit()
        self.token_url_e.setFixedWidth(560)
        self.token_url_e.textChanged.connect(self.upd_token)
        grid.addWidget(self.token_url_e, row, 1)
        row += 1

        msg = _('An optional hash hexidecimal bytes embedded into the token genesis transaction for hashing the document file contents at the URL provided above.')
        grid.addWidget(HelpLabel(_('Document Hash (optional):'), msg), row, 0)
        self.token_dochash_e = QLineEdit()
        self.token_dochash_e.setInputMask("HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHH")
        self.token_dochash_e.setFixedWidth(560)
        self.token_dochash_e.textChanged.connect(self.upd_token)
        grid.addWidget(self.token_dochash_e, row, 1)
        row += 1

        msg = _('Sets the number of decimals of divisibility for this token (embedded into genesis).') + '\n\n'\
              + _('Each 1 token is divisible into 10^(decimals) base units, and internally in the protocol the token amounts are represented as 64-bit integers measured in these base units.')
        grid.addWidget(HelpLabel(_('Decimal Places:'), msg), row, 0)
        self.token_ds_e = QDoubleSpinBox()
        self.token_ds_e.setRange(0, 9)
        self.token_ds_e.setDecimals(0)
        self.token_ds_e.setFixedWidth(50)
        self.token_ds_e.valueChanged.connect(self.upd_token)
        grid.addWidget(self.token_ds_e, row, 1)
        row += 1

        msg = _('The number of tokens created during token genesis transaction, send to the receiver address provided below.')
        grid.addWidget(HelpLabel(_('Token Quantity:'), msg), row, 0)
        self.token_qty_e = SLPAmountEdit('', 0)
        self.token_qty_e.setFixedWidth(200)
        self.token_qty_e.textChanged.connect(self.check_token_qty)
        grid.addWidget(self.token_qty_e, row, 1)
        row += 1

        msg = _('The \'simpleledger:\' formatted bitcoin address for the genesis receiver of all genesis tokens.')
        grid.addWidget(HelpLabel(_('Token Receiver Address:'), msg), row, 0)
        self.token_pay_to_e = ButtonsLineEdit()
        self.token_pay_to_e.setFixedWidth(560)
        grid.addWidget(self.token_pay_to_e, row, 1)
        try:
            slpAddr = self.wallet.get_unused_address().to_slpaddr()
            self.token_pay_to_e.setText(Address.prefix_from_address_string(slpAddr) + ":" + slpAddr)
        except:
            pass
        row += 1

        self.token_fixed_supply_cb = cb = QCheckBox(_('Fixed Supply'))
        self.token_fixed_supply_cb.setChecked(True)
        grid.addWidget(self.token_fixed_supply_cb, row, 1)
        cb.clicked.connect(self.show_mint_baton_address)
        row += 1

        msg = _('The \'simpleledger:\' formatted bitcoin address for the "minting baton" receiver.') + '\n\n'\
              + _('After the genesis transaction, further unlimited minting operations can be performed by the owner of the "minting baton" transaction output. This baton can be repeatedly used for minting operations but it cannot be duplicated.')
        self.token_baton_label = HelpLabel(_('Address for Baton:'), msg)
        self.token_baton_label.setHidden(True)
        grid.addWidget(self.token_baton_label, row, 0)
        self.token_baton_to_e = ButtonsLineEdit()
        self.token_baton_to_e.setFixedWidth(560)
        self.token_baton_to_e.setHidden(True)
        grid.addWidget(self.token_baton_to_e, row, 1)
        row += 1

        hbox = QHBoxLayout()
        vbox.addLayout(hbox)

        self.cancel_button = b = QPushButton(_("Cancel"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setDefault(False)
        b.clicked.connect(self.close)
        hbox.addWidget(self.cancel_button)

        hbox.addStretch(1)

        # self.hash_button = b = QPushButton(_("Compute Document Hash..."))
        # self.hash_button.setAutoDefault(False)
        # self.hash_button.setDefault(False)
        # b.clicked.connect(self.hash_file)
        # b.setDefault(True)
        # hbox.addWidget(self.hash_button)

        self.tok_doc_button = b = QPushButton(_("Upload a Token Document..."))
        self.tok_doc_button.setAutoDefault(False)
        self.tok_doc_button.setDefault(False)
        b.clicked.connect(self.show_upload)
        b.setDefault(True)
        hbox.addWidget(self.tok_doc_button)

        self.preview_button = EnterButton(_("Preview"), self.do_preview)
        self.create_button = b = QPushButton(_("Create New Token")) #if self.provided_token_name is None else _("Change"))
        b.clicked.connect(self.create_token)
        self.create_button.setAutoDefault(True)
        self.create_button.setDefault(True)
        hbox.addWidget(self.preview_button)
        hbox.addWidget(self.create_button)

        dialogs.append(self)
        self.show()
        self.token_name_e.setFocus()

    def show_upload(self):
        d = BitcoinFilesUploadDialog(self)
        dialogs.append(d)
        d.setModal(True)
        d.show()

    def do_preview(self):
        self.create_token(preview = True)

    def hash_file(self):
        options = QFileDialog.Options()
        options |= QFileDialog.DontUseNativeDialog
        filename, _ = QFileDialog.getOpenFileName(self,"Compute SHA256 For File", "","All Files (*)", options=options)
        if filename != '':
            with open(filename,"rb") as f:
                bytes = f.read() # read entire file as bytes
                import hashlib
                readable_hash = hashlib.sha256(bytes).hexdigest()
                self.token_dochash_e.setText(readable_hash)

    def upd_token(self,):
        self.token_qty_e.set_token(self.token_ticker_e.text(), int(self.token_ds_e.value()))

        # force update (will truncate excess decimals)
        self.token_qty_e.numbify()
        self.token_qty_e.update()
        self.check_token_qty()

    def show_mint_baton_address(self):
        self.token_baton_to_e.setHidden(self.token_fixed_supply_cb.isChecked())
        self.token_baton_label.setHidden(self.token_fixed_supply_cb.isChecked())

    def parse_address(self, address):
        if networks.net.SLPADDR_PREFIX not in address:
            address = networks.net.SLPADDR_PREFIX + ":" + address
        return Address.from_string(address)

    def create_token(self, preview=False):
        token_name = self.token_name_e.text() if self.token_name_e.text() != '' else None
        ticker = self.token_ticker_e.text() if self.token_ticker_e.text() != '' else None
        token_document_url = self.token_url_e.text() if self.token_url_e.text() != '' else None
        token_document_hash_hex = self.token_dochash_e.text() if self.token_dochash_e.text() != '' else None
        decimals = int(self.token_ds_e.value())
        mint_baton_vout = 2 if self.token_baton_to_e.text() != '' and not self.token_fixed_supply_cb.isChecked() else None

        init_mint_qty = self.token_qty_e.get_amount()
        if init_mint_qty is None:
            self.show_message(_("Invalid token quantity entered."))
            return
        if init_mint_qty > (2 ** 64) - 1:
            maxqty = format_satoshis_plain_nofloat((2 ** 64) - 1, decimals)
            self.show_message(_("Token output quantity is too large. Maximum %s.")%(maxqty,))
            return

        if token_document_hash_hex != None:
            if len(token_document_hash_hex) != 64:
                self.show_message(_("Token document hash must be a 32 byte hexidecimal string or left empty."))
                return

        outputs = []
        try:
            slp_op_return_msg = buildGenesisOpReturnOutput_V1(ticker, token_name, token_document_url, token_document_hash_hex, decimals, mint_baton_vout, init_mint_qty)
            outputs.append(slp_op_return_msg)
        except OPReturnTooLarge:
            self.show_message(_("Optional string text causiing OP_RETURN greater than 223 bytes."))
            return
        except Exception as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return

        try:
            addr = self.parse_address(self.token_pay_to_e.text())
            outputs.append((TYPE_ADDRESS, addr, 546))
        except:
            self.show_message(_("Must have Receiver Address in simpleledger format."))
            return

        if not self.token_fixed_supply_cb.isChecked():
            try:
                addr = self.parse_address(self.token_baton_to_e.text())
                outputs.append((TYPE_ADDRESS, addr, 546))
            except:
                self.show_message(_("Must have Baton Address in simpleledger format."))
                return

        # IMPORTANT: set wallet.sedn_slpTokenId to None to guard tokens during this transaction
        self.main_window.token_type_combo.setCurrentIndex(0)
        assert self.main_window.slp_token_id == None

        coins = self.main_window.get_coins()
        fee = None

        try:
            tx = self.main_window.wallet.make_unsigned_transaction(coins, outputs, self.main_window.config, fee, None)
        except NotEnoughFunds:
            self.show_message(_("Insufficient funds"))
            return
        except ExcessiveFee:
            self.show_message(_("Your fee is too high.  Max is 50 sat/byte."))
            return
        except BaseException as e:
            traceback.print_exc(file=sys.stdout)
            self.show_message(str(e))
            return

        if preview:
            show_transaction(tx, self.main_window, None, False, self)
            return

        msg = []

        if self.main_window.wallet.has_password():
            msg.append("")
            msg.append(_("Enter your password to proceed"))
            password = self.main_window.password_dialog('\n'.join(msg))
            if not password:
                return
        else:
            password = None
        tx_desc = None
        def sign_done(success):
            if success:
                if not tx.is_complete():
                    show_transaction(tx, self.main_window, None, False, self)
                    self.main_window.do_clear()
                else:
                    token_id = tx.txid()
                    if self.token_name_e.text() == '':
                        wallet_name = tx.txid()[0:5]
                    else:
                        wallet_name = self.token_name_e.text()[0:20]
                    # Check for duplication error
                    d = self.wallet.token_types.get(token_id)
                    for tid, d in self.wallet.token_types.items():
                        if d['name'] == wallet_name and tid != token_id:
                            wallet_name = wallet_name + "-" + token_id[:3]
                            break
                    self.broadcast_transaction(tx, self.token_name_e.text(), wallet_name)
        self.sign_tx_with_password(tx, sign_done, password)

    def sign_tx_with_password(self, tx, callback, password):
        '''Sign the transaction in a separate thread.  When done, calls
        the callback with a success code of True or False.
        '''
        # call hook to see if plugin needs gui interaction
        run_hook('sign_tx', self, tx)

        def on_signed(result):
            callback(True)

        def on_failed(exc_info):
            self.main_window.on_error(exc_info)
            callback(False)

        if self.main_window.tx_external_keypairs:
            task = partial(Transaction.sign, tx, self.main_window.tx_external_keypairs)
        else:
            task = partial(self.wallet.sign_transaction, tx, password)
        WaitingDialog(self, _('Signing transaction...'), task, on_signed, on_failed)

    def broadcast_transaction(self, tx, token_name, token_wallet_name):
        # Capture current TL window; override might be removed on return
        parent = self.top_level_window()
        if self.main_window.gui_object.warn_if_no_network(self):
            # Don't allow a useless broadcast when in offline mode. Previous to this we were getting an exception on broadcast.
            return
        elif not self.network.is_connected():
            # Don't allow a potentially very slow broadcast when obviously not connected.
            #parent.show_error(_("Not connected"))
            return

        def broadcast_thread():
            # non-GUI thread
            status = False
            msg = "Failed"
            status, msg =  self.network.broadcast_transaction(tx)
            return status, msg

        def broadcast_done(result):
            # GUI thread
            if result:
                status, msg = result
                if status:
                    token_id = msg
                    self.main_window.add_token_type('SLP1', token_id, token_wallet_name, int(self.token_ds_e.value()), allow_overwrite=True)
                    if tx.is_complete():
                        self.wallet.set_label(token_id, "SLP Token Created: " + token_wallet_name)
                    if token_name == '':
                        parent.show_message("SLP Token Created.\n\nName in wallet: " + token_wallet_name + "\nTokenId: " + token_id)
                    elif token_name != token_wallet_name:
                        parent.show_message("SLP Token Created.\n\nName in wallet: " + token_wallet_name + "\nName on blockchain: " + token_name + "\nTokenId: " + token_id)
                    else:
                        parent.show_message("SLP Token Created.\n\nName: " + token_name + "\nToken ID: " + token_id)
                else:
                    if msg.startswith("error: "):
                        msg = msg.split(" ", 1)[-1] # take the last part, sans the "error: " prefix
                    self.show_error(msg)
            self.close()

        WaitingDialog(self, 'Creating SLP Token...', broadcast_thread, broadcast_done, None)

    def closeEvent(self, event):
        self.main_window.create_token_dialog = None
        try:
            dialogs.remove(self)
        except ValueError:
            pass

    def update(self):
        return

    def check_token_qty(self):
        try:
            if self.token_qty_e.get_amount() > 18446744073709551615:
                self.token_qty_e.setAmount(18446744073709551615)
            #if not self.token_fixed_supply_cb.isChecked():
            #    self.show_warning(_("If you issue this much, users will may find it awkward to transfer large amounts, as each transaction output may only take up to ~" + str(self.token_qty_e.text()) + " tokens, thus requiring multiple outputs for very large amounts."))
        except:
            pass
