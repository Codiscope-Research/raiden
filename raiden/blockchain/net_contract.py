# -*- coding: utf8 -*-
"""
A pure python implementation of a contract responsable to open a channel.
"""
from ethereum import slogging

from raiden.utils import sha3, pex
from raiden.mtree import check_proof
from raiden.messages import MediatedTransfer, CancelTransfer, DirectTransfer, Lock, LockedTransfer
from raiden.encoding.messages import (
    DIRECTTRANSFER, LOCKEDTRANSFER, MEDIATEDTRANSFER, CANCELTRANSFER,
)

log = slogging.getLogger(__name__)  # pylint: disable=invalid-name


# Blockspam attack mitigation:
#     - Oracles, certifying, that previous blocks were full.
#     - Direct access to gasused of previous blocks.
#     - Heuristic, no settlements in the previous blocks.
# Todos:
#     Compatible Asset/Token/Coin Contract
#     Channel Opening sequence
#     Channel Fees (i.e. Accounts w/ higher reputation could charge a fee/deposit).
#     use channel.opened to collect reputation of an account (long lasting channels == good)


def is_newer_transfer(transfer, sender_state):
    """ Helper to check if `transfer` is from a newer block than
    sender_state's lastest transfer.
    """

    last_transfer = sender_state.get('last_sent_transfer')

    if last_transfer is None:
        return True

    return last_transfer.nonce < transfer.nonce


def tuple32(data):
    start = 0
    end = 8

    result = []
    while end <= len(data):
        pair = (data[start:end - 4], data[end - 4:end])
        result.append(pair)
        start = end
        end += 8

    return result


class NettingChannelContract(object):
    """ Contract that allows users to perform fast off-chain transactions.

    The netting contract allows two parties to engage in off-chain asset
    transfers without trust among them, with the functionality of detecting
    frauds, penalise the wrongdoers, and to participate in an off-chain network
    for fast and cheap transactions.

    Operation
    ---------

    Off-chain transactions are done by external clients without interaction
    with the channel's contract, the contract's role is only to secure the
    asset and create the mechanism that allows settlement of conflicts.

    The asset transfers are done through the exchange of signed messages among
    the participants, each message works as a proof of balance for a given
    participant at each moment. These messages are composed of:

        - The message signature, proving authenticity of the message.
        - The increasing counter `nonce`, identifying the order of the
        transfers.
        - The partner's current balance.
        - The merkle root of the locked transfers tree.
        - Possibly a `Lock` structure describing a new locked transfer.

    Since the contract does not mediate these off-chain transfers, it is the
    interest of participant to reject invalid messages, these are the points of
    concern:

        - Signatures need to be from a key recognized by the contract.
        - `Nonce`s are unique and increasing to identify the transfer order.
        - Negative transfers are invalid.
        - Maintain a correct merkle root with all non-expired locked transfer
        without a secret.
        - A valid timeout for `Lock`ed transfers.

    Transfers
    ---------

    There are two kinds of transfers that are recognized by the contract, a
    transfer initiate by a channel participant to the other participant, called
    a direct transfer, or a mediated transfer involving multiple channels, used
    for cooperatively transfer assets for nodes without a direct channel.

    Multiple transfers are expected to occur from the opening of a channel
    onwards, and only the latest with it's balance is valid. The `nonce` field
    is used by this contract to compare transfers and define which is the
    latest, it's responsability of each participant to reject messages with an
    decreasing or equal `nonce`, ensuring that this value is increasing, not
    necessarilly sequential/unitarily increasing.

    Direct Transfer
    ===============

    Direct transfers require only the exchange of a single signed message
    containing the current `nonce`, with an up-to-date balance and merkle
    proof.

    Mediated Transfer
    =================

    Direct transfer are possible only with the existence of a direct channel
    among the participants, since direct channels are expected to be the
    exception and not the rule a different mechanism is required for indirect
    transfers, this is done by exploiting existing channels to mediate a asset
    transfer. The path discovery required to find which channels will be used
    to mediate the transfer isn't part of this contract, only the means of
    protect the individual node.

    Mediated transfers require the participation of one or more intermediary
    nodes, these intermediaries compose a path from the initiator to the
    target. The path of length `n` has it's transfer started by the initiator
    `1`, with each intermediary `i` mediating a transfer from `i-1` to `i+1`
    until the the target node `n` is reached. This contract has the required
    mechanisms to protect the individual node's assets, the contract allows any
    `i` to safely transfer it's asset to `i+1` with the guarantee that it will
    have the transfer from `i-1` done.

    Note:
        Implementation in pure python that reproduces the expected behavior of
        the blockchain NettingContract. This implementation is useful for
        testing.
    """

    # The settle_timeout could be either fixed or variable:
    #
    # - For the fixed scenario, the application must not accept any locked
    # transfers that could expire after `settle_timeout` blocks, at the cost of
    # being susceptible to timming attacks.
    # - For the variable scenario, the `settle_timeout` would depend on the locked
    # transfer and to determine it's value a list of all the locks need to be
    # sent to the contract.
    #
    # This implementation uses a fixed lock time
    settle_timeout = 20
    """ Number of blocks that we are required to wait before allowing settlement. """

    def __init__(self, asset_address, netcontract_address, address_A, address_B):
        log.debug(
            'creating nettingchannelcontract',
            a=pex(address_A),
            b=pex(address_B),
        )

        self.asset_address = asset_address
        self.netcontract_address = netcontract_address
        self.participants = {
            address_A: dict(deposit=0, last_sent_transfer=None, unlocked=[]),
            address_B: dict(deposit=0, last_sent_transfer=None, unlocked=[]),
        }
        self.hashlocks = dict()

        self.opened = None
        """ Block number when deposit() was first called. """

        self.settled = False
        """ Block number when settle was sucessfully called. """

        self.closed = None
        """ Block number when close() was first called (might be zero in testing scenarios) """

    @property
    def isopen(self):
        """ The contract is open after both participants have deposited, and if
        it has not being closed.

        Returns:
            bool: True if the contract is open, False otherwise
        """
        # during testing closed can be 0 and it is falsy
        if self.closed is not None:
            return False

        lowest_deposit = min(
            state['deposit']
            for state in self.participants.values()
        )

        all_deposited = lowest_deposit > 0
        return all_deposited

    def deposit(self, address, amount, block_number):
        """ Deposit `amount` coins for the address `address`. """

        if address not in self.participants:
            msg = 'The address {address} is not a participant of this contract'.format(
                address=address,
            )

            log.debug('unknow address', address=address, participants=self.participants)

            raise ValueError(msg)

        if amount < 0:
            raise ValueError('amount cannot be negative')

        self.participants[address]['deposit'] += amount

        if self.isopen and self.opened is None:
            # track the block were the contract was openned
            self.opened = block_number

    def partner(self, address):
        """ Returns the address of the other participant in the contract. """

        if address not in self.participants:
            msg = 'The address {address} is not a participant of this contract'.format(
                address=pex(address),
            )
            raise ValueError(msg)

        all_participants = list(self.participants.keys())
        all_participants.remove(address)
        return all_participants[0]

    def close(self, ctx, sender, transfers_encoded, locked_encoded,  # noqa
              merkleproof_encoded, secret):
        """" Request the closing of the channel. Can be called multiple times.
        lock period starts with first valid call.

        Args:
            sender (address):
                The sender address.

            transfers_encoded (List[transfer]):
                A list of maximum length of 2 containing the transfer encoded
                using the fixed length format, may be empty.

            ctx:
                Block chain state used for mocking.

            locked_encoded (bin):
                The Lock to be unlocked.

            merkleproof_encoded (bin):
                A proof that the given lock is contained in the latest
                transfer. The binary data is composed of a single hash at every
                4bytes.

            secret (bin):
                The secret that unlocks the lock `hashlock = sha3(secret)`.

        Todo:
            if challenged, keep track of who provided the last valid answer,
            punish the wrongdoer here, check that participants only updates
            their own balance are counted, because they could sign something
            for the other party to blame it.
        """
        # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
        # if len(transfers_encoded):
        #     raise ValueError('transfers_encoded needs at least 1 item.')

        if len(transfers_encoded) > 2:
            raise ValueError('transfers_encoded cannot have more than 2 items.')

        if self.settled:
            raise RuntimeError('contract is settled')

        # the merkleproof can be empty, if there is only one haslock
        has_oneofunlocked = locked_encoded or secret
        has_allofunlocked = locked_encoded and secret
        if has_oneofunlocked and not has_allofunlocked:
            raise ValueError(
                'all arguments `merkle_proof`, `locked`, and `secret` must be provided'
            )

        last_sent_transfers = []
        for data in transfers_encoded:
            if data[0] == DIRECTTRANSFER:
                last_sent_transfers.append(
                    DirectTransfer.decode(data)
                )
            elif data[0] == MEDIATEDTRANSFER:
                last_sent_transfers.append(
                    MediatedTransfer.decode(data)
                )
            elif data[0] == CANCELTRANSFER:
                last_sent_transfers.append(
                    CancelTransfer.decode(data)
                )
            # convinience for testing only (LockedTransfer are not exchanged between nodes)
            elif data[0] == LOCKEDTRANSFER:
                last_sent_transfers.append(
                    LockedTransfer.decode(data)
                )
            else:
                raise ValueError('invalid transfer type {}'.format(type(data[0])))

        # keep the latest claim
        for transfer in last_sent_transfers:
            if transfer.sender not in self.participants:
                raise ValueError('Invalid tansfer, sender is not a participant')

            sender_state = self.participants[transfer.sender]

            if is_newer_transfer(transfer, sender_state):
                sender_state['last_sent_transfer'] = transfer

        partner = self.partner(sender)
        partner_state = self.participants[partner]

        if last_sent_transfers:
            transfer = last_sent_transfers[-1]  # XXX: check me

        # register un-locked
        if merkleproof_encoded:
            merkle_proof = tuple32(merkleproof_encoded)
            lock = Lock.from_bytes(locked_encoded)

            hashlock = lock.hashlock
            if hashlock != sha3(secret):
                raise ValueError('invalid secret')

            # the partner might not have made a transfer
            if partner_state['last_sent_transfer'] is not None:
                assert check_proof(
                    merkle_proof,
                    partner_state['last_sent_transfer'].locksroot,
                    sha3(transfer.lock.as_bytes),
                )

            partner_state['unlocked'].append(lock)

        if self.closed is None:
            log.debug('closing contract', netcontract_address=pex(self.netcontract_address))
            self.closed = ctx['block_number']

    def settle(self, ctx):
        assert not self.settled
        # during testing closed can be 0 and it is falsy
        assert self.closed is not None
        assert self.closed + self.settle_timeout <= ctx['block_number']

        for address, state in self.participants.items():
            other = self.participants[self.partner(address)]
            state['netted'] = state['deposit']

            # FIXME: use the latest transfer only
            if state.get('last_sent_transfer'):
                state['netted'] = state['last_sent_transfer'].balance

            if other.get('last_sent_transfer'):
                state['netted'] = other['last_sent_transfer'].balance

        # add locked
        for address, state in self.participants.items():
            other = self.participants[self.partner(address)]
            for locked in state['unlocked']:
                state['netted'] += locked.amount
                other['netted'] -= locked.amount

        total_netted = sum(state['netted'] for state in self.participants.values())
        total_deposit = sum(state['deposit'] for state in self.participants.values())

        assert total_netted <= total_deposit

        self.settled = ctx['block_number']

        return dict(
            (address, state['netted'])
            for address, state in self.participants.items()
        )
